"""
Command MCP - 커맨드 카탈로그를 **툴로 노출**하고 실행한다.

카탈로그(command_catalog)의 행 하나가 MCP 툴 하나가 된다. LLM은 툴 목록과 설명을 직접 보고
고른다 - System MCP와 같은 방식이다. 예전에는 하이브리드 검색(search_commands)으로 후보를
넘겼는데, 검색이 한 번 어긋나면 등록해 둔 커맨드가 없는 것처럼 취급되는 문제가 반복됐다.
카탈로그에 없는 커맨드(매뉴얼 문서에서 찾은 것 등)는 run_command로 그대로 실행한다.

모든 실행 툴은 user_scoped=True로, user_id를 LLM 스키마에서 감추고 호출자 신원에서
강제 주입한다(남의 자원에 접근할 수 없다).

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
# 같은 폴더의 catalog_tools를 어떤 로더로 불러도 찾을 수 있게 한다.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from db import get_pool  # noqa: E402
from config_store import get_config  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, load_overrides_sync, tool_description, build_wrapped,
)
from ssh_exec import (  # noqa: E402
    run_ssh_as_user, warm_master, start_master_keepalive,
)
from catalog_exec import DEFAULT_DENY_CSV, build_catalog_argv, deny_set  # noqa: E402
from catalog_tools import estimate_prompt_tokens, load_catalog_tools_sync  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("command-mcp", stateless_http=True, host="0.0.0.0")

_DSN = "command_db_dsn"
_STATE = "command_whitelist_state"


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
    target = (host or "").strip() or await get_config("scheduler_login_host", "202.20.185.100")

    result = await run_ssh_as_user(target, user_id, argv)
    result["source"] = source
    return result


# 실행 툴 목록. System MCP의 WHITELIST와 달리 여기 등록된 툴은 항상 실행 가능하다
# (enabled/required_roles로 막지 않는다 - 화이트리스트 관리는 System MCP 전용 정책).
EXEC_TOOLS = {
    "run_command": {
        "handler": run_command,
        "description": (
            "임의의 사내 커맨드를 실행한다. 카탈로그 커맨드는 각각 전용 툴(cmd_*)로 나와 있으니 "
            "**그 툴을 먼저 쓰고**, 목록에 없는 커맨드(매뉴얼에서 찾은 것 등)만 이 툴로 실행한다. "
            "args는 인자를 한 칸씩 나눠 넣는다(예: ['-l','/home']). "
            "host는 사용자가 특정 서버를 지목했을 때만 넣는다(기본: 로그인 서버)."
        ),
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
}
# job 조회 전용 툴은 코드에 두지 않는다. 예전에는 `phd info -u <user>`를 코드(그 다음엔
# 설정값)에 박아 뒀는데, 그러면 관리자가 콘솔에서 커맨드를 고쳐도 반영되지 않는다.
# 스케줄러 커맨드도 다른 사내 커맨드와 똑같이 **카탈로그에 등록**하면 아래에서 툴로 노출된다.
# 카탈로그가 유일한 출처다.

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


async def _login_host() -> str:
    return await get_config("scheduler_login_host", "202.20.185.100")


# 카탈로그의 행 하나 = 툴 하나. 기동 시 1회 읽는다(커맨드를 고치면 이 MCP를 재시작해야 한다).
CATALOG_TOOLS, _DROPPED = load_catalog_tools_sync(_login_host)
if _DROPPED:
    print(f"[command-mcp] 카탈로그가 상한(command_tools_max)을 넘어 {_DROPPED}개를 툴로 "
          "노출하지 못했습니다. 설정에서 상한을 올리거나 쓰지 않는 커맨드를 정리하세요 "
          "- 노출되지 않은 커맨드는 run_command로만 실행할 수 있습니다.")
_ALL_TOOLS = {**EXEC_TOOLS, **CATALOG_TOOLS}
_CHARS, _TOKENS = estimate_prompt_tokens(
    [tool_description(n, e, _OVERRIDES) for n, e in _ALL_TOOLS.items()])
# 툴 설명은 **매 요청** 프롬프트에 통째로 실린다. LLM 컨텍스트가 32768이라 카탈로그가 커지면
# 검색 결과·대화 이력에 쓸 자리가 줄어든다. 기동할 때 실제 비용을 찍어 두면
# "커맨드를 몇 개까지 등록해도 되나"를 감이 아니라 숫자로 판단할 수 있다.
print(f"[command-mcp] 카탈로그 커맨드 {len(CATALOG_TOOLS)}개를 툴로 노출합니다 "
      f"(툴 {len(_ALL_TOOLS)}개 · 스키마 {_CHARS:,}자 ≈ {_TOKENS:,}토큰/요청).")
if _TOKENS > 6000:
    print(f"[command-mcp] 경고: 툴 스키마가 요청마다 약 {_TOKENS:,}토큰을 씁니다. 지시문(~4.9k)과 "
          "합치면 32768 컨텍스트의 3분의 1이 넘어 검색 결과·대화 이력이 밀려납니다. "
          "커맨드 설명을 한 줄로 줄이거나 안 쓰는 커맨드를 정리하세요.")

for _name, _entry in _ALL_TOOLS.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, is_enabled=_always_enabled,
                      required_roles=_no_required_roles, log_execution=_log_execution),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8002))
    inner = mcp.streamable_http_app()

    # 기동하자마자 로그인 서버로 ssh 마스터 연결을 열어 둔다. 사용자가 첫 질문을 던질 때
    # 이미 연결이 서 있으므로 커맨드가 곧바로 실행된다(첫 접속 비용이 체감 지연의 대부분).
    # 이후 ControlPersist가 끊기지 않게 주기적으로 다시 예열한다.
    async def _warm_ssh():
        try:
            host = await _login_host()
        except Exception as e:  # noqa: BLE001
            print(f"[command-mcp] 로그인 서버 설정을 읽지 못해 예열을 건너뜁니다: {e}")
            return
        if host:
            await warm_master(host)
        start_master_keepalive(lambda: get_config("scheduler_login_host", host))

    inner.add_event_handler("startup", _warm_ssh)
    uvicorn.run(CallerContextMiddleware(inner), host="0.0.0.0", port=port)
