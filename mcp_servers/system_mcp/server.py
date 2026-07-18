"""
System MCP - 화이트리스트(read-only 리눅스 명령)만 실행하는 MCP 서버.
whitelist.py에 없는 동작은 절대 수행하지 않는다. 각 툴은 셸 없이 argv로 실행되고(주입 불가),
호출자(user_id) 권한으로 강등되어 실행된다(linux_exec).

호출자 컨텍스트/권한검사/감사로그/ user_id 주입 로직은 shared/mcp_caller로 공통화한다.
- enabled(활성)/required_roles(필요 역할)는 실행 시점에 system_db에서 실시간으로 읽는다.
- description_override(LLM에 보이는 설명)는 기동 시 1회 읽어 반영한다(변경은 MCP 재시작 필요).
- 모든 실행은 job_logs에 호출자와 함께 감사 기록된다.
"""
import sys
import os
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, load_overrides_sync, tool_description, build_wrapped,
)
from whitelist import WHITELIST  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("system-mcp", stateless_http=True)

_DSN = "system_db_dsn"
_STATE = "system_whitelist_state"
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
    row = await pool.fetchrow(
        f"SELECT enabled FROM {_STATE} WHERE tool_name = $1", tool_name)
    if row is None:
        await pool.execute(
            f"INSERT INTO {_STATE} (tool_name, enabled) VALUES ($1, $2) "
            "ON CONFLICT (tool_name) DO NOTHING",
            tool_name, default)
        return default
    return row["enabled"]


async def _required_roles(tool_name: str, code_default: list) -> list:
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        f"SELECT required_roles FROM {_STATE} WHERE tool_name = $1", tool_name)
    if row and row["required_roles"] is not None:
        return list(row["required_roles"])
    return list(code_default or [])


for _name, _entry in WHITELIST.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, is_enabled=_is_enabled,
                      required_roles=_required_roles, log_execution=_log_execution),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8004))
    app = CallerContextMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
