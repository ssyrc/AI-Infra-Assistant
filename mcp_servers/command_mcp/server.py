"""
Command MCP - 두 가지 역할.
1) 커맨드 카탈로그 조회: "어떤 커맨드가 있고 어떻게 쓰는지" 하이브리드 의미검색(읽기 전용).
2) 실행: 카탈로그에 등록된 커맨드(run_command)와 스케줄러 job 조회 등 '본인' 자원에 대한
   애플리케이션 명령 실행. 모든 실행 툴은 user_scoped=True로, user_id를 LLM 스키마에서 감추고
   호출자 신원에서 강제 주입한다(남의 자원에 접근할 수 없다).

화이트리스트 정책(관리자 결정): Command MCP의 툴은 항목별 on/off 없이 항상 실행 가능하다.
  실행 허용 여부를 관리자가 켜고 끄는 '화이트리스트 관리'는 System MCP에서만 한다.
  Command MCP에서는 권한 검사 대신 (a) 호출자 권한 강등 실행, (b) 셸 미사용 argv 실행,
  (c) 파괴적 기본 명령 거부(shared/catalog_exec), (d) 전건 감사로그로 안전성을 확보한다.

전용 DB(command_db)를 사용한다.
"""
import sys
import os
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402
from config_store import get_config  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, load_overrides_sync, tool_description, build_wrapped,
)
from ssh_exec import run_ssh_as_user  # noqa: E402
from catalog_exec import DEFAULT_DENY_CSV, build_catalog_argv, deny_set  # noqa: E402
from retrieval import (  # noqa: E402
    ts_or_query, expand_query, has_trgm, mmr_dedup, trgm_min_similarity,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("command-mcp", stateless_http=True, host="0.0.0.0")

_DSN = "command_db_dsn"
_STATE = "command_whitelist_state"


# ------------------------------------------------------------------ 카탈로그 검색
@mcp.tool()
async def search_commands(query: str, top_k: int = 10) -> list[dict]:
    """하려는 작업을 설명하면 의미상 가까운 사내 시스템 커맨드를 찾아 준다(카탈로그 조회).

    사용할 때: "무슨 커맨드로 X를 하지?"처럼 어떤 명령이 있는지 모를 때, 그리고 사용자가
      요청한 작업을 실제로 수행할 커맨드를 찾을 때. 정확한 이름이 없어도 설명형으로 검색된다.
      예: "내 홈 스토리지 용량", "작업이 언제 실행되는지 확인".

    이 툴 자체는 실행하지 않는다. 찾은 커맨드를 실제로 실행하려면 결과의 name을 그대로
    run_command(name=...)에 넘긴다(카탈로그에 있는 커맨드는 전부 실행 가능하다).

    Args:
        query: 하려는 작업 설명 또는 키워드. 예: "배치 재시작"
        top_k: 반환할 최대 건수(기본 10)
    Returns:
        커맨드 리스트. 각 항목에 name, description, command(실제 실행되는 커맨드)가 있다.
    """
    if not query or not query.strip():
        return []
    top_k = await clamp_top_k(top_k)
    candidate_k = await clamp_candidates(top_k * 5)
    pool = await get_pool(_DSN)

    vec = None
    try:
        vec = await embed_text(query)
    except Exception as e:  # noqa: BLE001
        print(f"[command-mcp] 임베딩 실패, 키워드 검색으로 fallback: {type(e).__name__}: {e}")

    variants = expand_query(query)
    ts_query = ts_or_query(" ".join(variants)) or ts_or_query(query) or "''"
    use_trgm = await has_trgm(pool, _DSN)

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, name, description, exec_command,
                   ts_rank(tsv, to_tsquery('simple', $1)) AS score
            FROM command_catalog
            WHERE tsv @@ to_tsquery('simple', $1)
               OR name ILIKE '%' || $2 || '%'
               OR description ILIKE '%' || $2 || '%'
            ORDER BY score DESC
            LIMIT $3
            """,
            ts_query, query, candidate_k,
        )
    elif use_trgm:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM command_catalog WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $2)) DESC) AS rank
                FROM command_catalog WHERE tsv @@ to_tsquery('simple', $2) LIMIT 50
            ),
            trgm_search AS (
                -- similarity()가 아니라 word_similarity(). similarity는 두 문자열 '전체'의
                -- 3-gram 자카드라 설명이 길수록 0에 수렴해 임계값 0.3을 넘지 못한다
                -- (= 이 축이 항상 0건이었다). word_similarity는 '설명 안에서 질의와 가장
                -- 잘 맞는 구간'을 보므로 길이에 휘둘리지 않는다.
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY word_similarity($3, name || ' ' || description) DESC) AS rank
                FROM command_catalog
                WHERE word_similarity($3, name || ' ' || description) >= $4
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id, t.id) AS id,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0)
                       + COALESCE(1.0/(60+t.rank),0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
                FULL OUTER JOIN trgm_search t ON COALESCE(v.id, k.id) = t.id
            )
            SELECT c.id, c.name, c.description, c.exec_command, fused.rrf_score AS score
            FROM fused JOIN command_catalog c ON c.id = fused.id
            ORDER BY fused.rrf_score DESC LIMIT $5
            """,
            vector_literal(vec), ts_query, query, await trgm_min_similarity(), candidate_k,
        )
    else:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM command_catalog
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $2)) DESC
                ) AS rank
                FROM command_catalog
                WHERE tsv @@ to_tsquery('simple', $2)
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id,
                       COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT c.id, c.name, c.description, c.exec_command,
                   fused.rrf_score AS score
            FROM fused
            JOIN command_catalog c ON c.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $3
            """,
            vector_literal(vec), ts_query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    docs = [f"{c['name']}\n{c['description']}" for c in candidates]
    ranked = await rerank(query, docs, top_k * 2)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        # 실행 커맨드가 따로 없으면 이름이 그대로 실행된다 - LLM에는 실제 실행될 것만 보인다.
        item["command"] = (item.pop("exec_command", None) or item["name"])
        item["rerank_score"] = rr_score
        result.append(item)
    return mmr_dedup(result, lambda c: f"{c['name']} {c['description']}", top_k, 0.9)


@mcp.tool()
async def get_command_detail(name: str) -> dict | None:
    """특정 커맨드의 설명과 실제 실행될 커맨드를 정확히 반환한다.

    사용할 때: search_commands 결과만으로 확신이 안 설 때 한 건을 정확히 확인할 때.
      name은 반드시 search_commands 결과의 name을 그대로 쓴다.

    Args:
        name: command_catalog.name 값(추측 금지, 검색 결과의 정확한 이름)
    Returns:
        name/description/command. command는 실제로 실행되는 커맨드 문자열이다.
        없으면 null(그때는 search_commands로 다시 찾는다).
    """
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        "SELECT name, description, exec_command FROM command_catalog WHERE name = $1", name)
    if row is None:
        return None
    d = dict(row)
    d["command"] = d.pop("exec_command", None) or d["name"]
    return d


# ------------------------------------------------------------------ 사용자 스코프 실행
async def run_command(user_id: str, command: str, args: list[str] | None = None,
                      host: str | None = None) -> dict:
    """커맨드를 호출자 본인 권한으로 실행한다. 출처(카탈로그/매뉴얼)를 가리지 않는다.

    command가 카탈로그(command_catalog)에 등록된 이름이면 그 exec_command를 쓰고,
    아니면 받은 문자열을 그대로 커맨드로 본다(매뉴얼 문서에서 찾은 커맨드를 등록 없이 실행하기
    위함). 어느 쪽이든 셸 없이 argv로 분해해 실행하고, 파괴적 기본 명령은 거부한다.
    host를 지정하지 않으면 로그인 서버(scheduler_login_host)에서 실행한다."""
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        "SELECT name, exec_command FROM command_catalog WHERE name = $1", command)
    if row is None:
        # 대소문자/앞뒤 공백 차이는 흡수한다(LLM이 검색 결과를 그대로 넘기지 못한 경우).
        row = await pool.fetchrow(
            "SELECT name, exec_command FROM command_catalog "
            "WHERE lower(name) = lower(btrim($1))", command)

    deny = deny_set(await get_config("catalog_exec_deny_commands", DEFAULT_DENY_CSV))
    if row is not None:
        source = "카탈로그"
        argv = build_catalog_argv(row["exec_command"], row["name"], args, user_id, deny)
    else:
        # 카탈로그에 없는 커맨드(매뉴얼/VOC에서 찾은 것 등)도 그대로 실행한다.
        source = "직접 지정"
        argv = build_catalog_argv(command, command, args, user_id, deny)
    target = (host or "").strip() or await get_config("scheduler_login_host", "login05")

    result = await run_ssh_as_user(target, user_id, argv)
    result["source"] = source
    return result


async def get_scheduler_job_info(user_id: str) -> dict:
    """현재 사용자 '본인'의 스케줄러 job 상태를 조회한다.
    로그인 서버(scheduler_login_host)에 ssh(root) 후 `su - <user_id>`로 강등해
    `phd info -u <user_id>`를 실행한다. user_id는 호출자 신원에서 강제 주입되므로
    남의 job을 조회할 수 없다. 결과에 실행한 커맨드(command)도 함께 반환한다."""
    login_host = await get_config("scheduler_login_host", "login05")
    return await run_ssh_as_user(login_host, user_id, ["phd", "info", "-u", user_id])


# 실행 툴 목록. System MCP의 WHITELIST와 달리 여기 등록된 툴은 항상 실행 가능하다
# (enabled/required_roles로 막지 않는다 - 화이트리스트 관리는 System MCP 전용 정책).
EXEC_TOOLS = {
    "run_command": {
        "handler": run_command,
        "description": (
            "사내 커맨드를 실제로 실행하고 결과를 돌려준다. 사용자가 어떤 정보를 '확인해 달라'고 "
            "하면 사용법만 안내하지 말고 이 툴로 실행해서 결과로 답한다. "
            "command에는 실행할 커맨드를 넣는다 — **커맨드 카탈로그(search_commands)에서 찾은 "
            "이름이든, 매뉴얼 문서(manual.search_manual)에서 찾은 커맨드든 상관없이** 그대로 "
            "넘기면 실행된다(별도 등록이 필요 없다). 다만 반드시 검색 결과에 실제로 있던 커맨드만 "
            "쓰고, 없는 커맨드를 지어내지 않는다. "
            "args에는 커맨드 뒤에 붙일 인자를 한 칸씩 나눠 넣는다(예: ['-l', '/home']). 인자가 "
            "필요 없으면 생략한다(command에 인자까지 함께 적어도 된다). "
            "host를 지정하지 않으면 로그인 서버에서 실행되며, 사용자가 특정 서버(예: hgpu4041)를 "
            "지목한 경우에만 그 서버 이름을 host에 넣는다. "
            "실행은 항상 호출자 본인 계정 권한으로 이뤄진다(사용자 id는 지정할 수 없다). "
            "파일 삭제 등 파괴적 명령은 시스템이 거부한다."
        ),
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
    "get_scheduler_job_info": {
        "handler": get_scheduler_job_info,
        "description": (
            "현재 로그인한 사용자 '본인'의 스케줄러 job 상태를 조회한다(로그인 서버에서 "
            "`phd info -u <본인>` 실행). 사용자가 '내 job', '내 작업 상태'를 물을 때 사용한다. "
            "대상 사용자는 시스템이 본인으로 고정하므로 사용자 id를 지정하지 않는다(남의 job 불가). "
            "커맨드 '사용법'만 궁금하면 이 툴 대신 search_commands를 쓴다."
        ),
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
}

# 설명 오버라이드만 기동 시 1회 읽는다(enabled/required_roles는 더 이상 참조하지 않는다).
_OVERRIDES = load_overrides_sync(_DSN, _STATE)


async def _log_execution(tool_name: str, params: dict, status: str, result):
    caller = get_caller()
    pool = await get_pool(_DSN)
    await pool.execute(
        """
        INSERT INTO job_logs (tool_name, params, requested_by, status, result,
                              conversation_id, request_id)
        VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6, $7)
        """,
        tool_name,
        json.dumps(params, ensure_ascii=False, default=str),
        caller.get("user_id") or "unknown",
        status,
        json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
        caller.get("conversation_id"),
        caller.get("request_id"),
    )


async def _always_enabled(tool_name: str, default: bool) -> bool:
    """Command MCP는 항목별 활성/비활성을 두지 않는다(화이트리스트 관리는 System MCP 전용)."""
    return True


async def _no_required_roles(tool_name: str, code_default: list) -> list:
    """역할 제한도 두지 않는다 - 실행은 항상 호출자 '본인' 권한으로만 이뤄진다."""
    return []


for _name, _entry in EXEC_TOOLS.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, is_enabled=_always_enabled,
                      required_roles=_no_required_roles, log_execution=_log_execution),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8002))
    app = CallerContextMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
