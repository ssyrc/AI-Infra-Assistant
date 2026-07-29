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
from config_store import get_config  # noqa: E402
from ssh_exec import warm_master, start_master_keepalive  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, load_overrides_sync, tool_description, build_wrapped,
)
from whitelist import WHITELIST as _CODE_WHITELIST  # noqa: E402
from custom_whitelist import load_custom_whitelist_sync  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("system-mcp", stateless_http=True, host="0.0.0.0")

_DSN = "system_db_dsn"
_STATE = "system_whitelist_state"
# host_mode는 required_roles/enabled와 달리 LLM 스키마(host 파라미터 노출 여부)에 영향을 줘서
# 기동 시 1회만 반영한다(설명 오버라이드와 동일한 제약 — 변경 후 System MCP 재시작 필요).
_OVERRIDES = load_overrides_sync(_DSN, _STATE, extra_columns=("host_mode",))

# 코드 내장 화이트리스트 + 관리자 콘솔에서 등록한 커맨드(system_custom_commands)를 병합한다.
# 콘솔 등록 커맨드도 기동 시 1회만 반영된다(추가/수정 후 System MCP 재시작 필요).
WHITELIST = {**_CODE_WHITELIST, **load_custom_whitelist_sync(_DSN)}

# 코드 내장 항목의 host_mode는 관리자 콘솔에서 바꾼 값(system_whitelist_state)이 있으면 그걸 따른다.
# 커스텀 커맨드는 자기 테이블(system_custom_commands)의 host_mode를 이미 갖고 있어 그대로 둔다.
for _name in _CODE_WHITELIST:
    _ov = _OVERRIDES.get(_name) or {}
    if _ov.get("host_mode"):
        WHITELIST[_name] = {**WHITELIST[_name], "host_mode": _ov["host_mode"]}


async def _login_host() -> str:
    return await get_config("scheduler_login_host", "202.20.185.100")


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
                      required_roles=_required_roles, log_execution=_log_execution,
                      host_mode=_entry.get("host_mode", "target_server"),
                      login_host=_login_host),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8004))
    inner = mcp.streamable_http_app()

    # 기동하자마자 로그인 서버로 ssh 마스터 연결을 열어 둔다. 사용자가 첫 질문을 던질 때
    # 이미 연결이 서 있으므로 커맨드가 곧바로 실행된다(첫 접속 비용이 체감 지연의 대부분).
    # 이후 ControlPersist가 끊기지 않게 주기적으로 다시 예열한다.
    async def _warm_ssh():
        try:
            host = await get_config("scheduler_login_host", "202.20.185.100")
        except Exception as e:  # noqa: BLE001
            print(f"[system-mcp] 로그인 서버 설정을 읽지 못해 예열을 건너뜁니다: {{e}}")
            return
        if host:
            await warm_master(host)
        start_master_keepalive(lambda: get_config("scheduler_login_host", host))

    inner.add_event_handler("startup", _warm_ssh)
    uvicorn.run(CallerContextMiddleware(inner), host="0.0.0.0", port=port)
