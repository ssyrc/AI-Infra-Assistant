"""
Command MCP - 두 가지 역할.
1) 커맨드 카탈로그 조회: "어떤 커맨드가 있고 어떻게 쓰는지" 하이브리드 의미검색(읽기 전용).
2) 사용자 스코프 실행: 스케줄러 job 등 '본인' 자원에 대한 애플리케이션 명령 실행.
   실행 툴은 user_scoped=True로, user_id를 LLM 스키마에서 감추고 호출자 신원에서 강제 주입한다
   (남의 job을 조회할 수 없다). enabled/역할/감사로그는 shared/mcp_caller로 공통 처리한다.
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

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("command-mcp", stateless_http=True)

_DSN = "command_db_dsn"
_STATE = "command_whitelist_state"


# ------------------------------------------------------------------ 카탈로그 검색
@mcp.tool()
async def search_commands(query: str, top_k: int = 10, category: str | None = None) -> list[dict]:
    """하고 싶은 작업을 설명하면 의미상 가까운 시스템 커맨드를 찾아 준다.

    정확한 커맨드명이 아니어도 된다. 예) "작업이 언제 실행되는지 확인", "스케줄 등록".

    Args:
        query: 하고 싶은 작업 설명 또는 검색어
        top_k: 반환할 최대 건수 (기본 10)
        category: 특정 카테고리로 필터링 (없으면 전체)
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

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, name, description, usage, category,
                   ts_rank(tsv, plainto_tsquery('simple', $1)) AS score
            FROM command_catalog
            WHERE ($2::text IS NULL OR category = $2)
              AND (tsv @@ plainto_tsquery('simple', $1)
                   OR name ILIKE '%' || $1 || '%'
                   OR description ILIKE '%' || $1 || '%')
            ORDER BY score DESC
            LIMIT $3
            """,
            query, category, candidate_k,
        )
    else:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM command_catalog
                WHERE ($2::text IS NULL OR category = $2)
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, plainto_tsquery('simple', $3)) DESC
                ) AS rank
                FROM command_catalog
                WHERE ($2::text IS NULL OR category = $2)
                  AND tsv @@ plainto_tsquery('simple', $3)
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id,
                       COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT c.id, c.name, c.description, c.usage, c.category,
                   fused.rrf_score AS score
            FROM fused
            JOIN command_catalog c ON c.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $4
            """,
            vector_literal(vec), category, query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    docs = [f"{c['name']}\n{c['description']}" for c in candidates]
    ranked = await rerank(query, docs, top_k)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        result.append(item)
    return result


@mcp.tool()
async def get_command_detail(name: str) -> dict | None:
    """특정 커맨드의 상세 사용법을 반환한다.

    Args:
        name: command_catalog.name 값
    """
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        "SELECT name, description, usage, category FROM command_catalog WHERE name = $1",
        name,
    )
    return dict(row) if row else None


# ------------------------------------------------------------------ 사용자 스코프 실행
async def get_scheduler_job_info(user_id: str) -> dict:
    """현재 사용자 '본인'의 스케줄러 job 정보를 조회한다.
    user_id는 호출자 신원(X-User-Id)에서 강제 주입되므로 남의 job을 조회할 수 없다."""
    base_url = await get_config("scheduler_api_base_url")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base_url}/jobs", params={"user_id": user_id})
        resp.raise_for_status()
        return resp.json()


async def get_scheduler_queue_status() -> dict:
    """s2 스케줄러 큐의 전체 대기/실행 상태를 조회한다(사용자별 데이터 아님)."""
    base_url = await get_config("scheduler_api_base_url")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base_url}/queue/status")
        resp.raise_for_status()
        return resp.json()


EXEC_WHITELIST = {
    "get_scheduler_job_info": {
        "handler": get_scheduler_job_info,
        "description": "현재 사용자 본인의 스케줄러 job 정보를 조회한다.",
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
    "get_scheduler_queue_status": {
        "handler": get_scheduler_queue_status,
        "description": "s2 스케줄러 큐의 전체 대기/실행 상태를 조회한다(사용자별 데이터 아님).",
        "enabled": True, "required_roles": [], "user_scoped": False,
    },
}

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


async def _is_enabled(tool_name: str, default: bool) -> bool:
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(f"SELECT enabled FROM {_STATE} WHERE tool_name = $1", tool_name)
    if row is None:
        await pool.execute(
            f"INSERT INTO {_STATE} (tool_name, enabled) VALUES ($1, $2) "
            "ON CONFLICT (tool_name) DO NOTHING", tool_name, default)
        return default
    return row["enabled"]


async def _required_roles(tool_name: str, code_default: list) -> list:
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(f"SELECT required_roles FROM {_STATE} WHERE tool_name = $1", tool_name)
    if row and row["required_roles"] is not None:
        return list(row["required_roles"])
    return list(code_default or [])


for _name, _entry in EXEC_WHITELIST.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, is_enabled=_is_enabled,
                      required_roles=_required_roles, log_execution=_log_execution),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8002))
    app = CallerContextMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
