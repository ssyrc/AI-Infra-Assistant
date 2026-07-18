"""
System MCP - 화이트리스트에 등록된 함수만 실행 가능한 MCP 서버.
whitelist.py에 없는 동작은 절대 수행하지 않는다 (임의 셸 실행 없음).

보안 설계:
- functools.wraps로 원본 함수 시그니처를 보존한다. 이렇게 해야 MCP input schema에
  user_id 같은 실제 파라미터가 정확히 노출되어 LLM이 올바르게 호출할 수 있다.
- 호출자 정보(사용자 ID/대화 ID/요청 ID)는 HTTP 헤더로 전달받아 감사로그에 남긴다.
  Agent Server가 X-User-Id / X-Conversation-Id / X-Request-Id 헤더를 붙여준다.
- 화이트리스트 항목의 required_roles가 지정돼 있으면 호출자 역할(X-User-Roles)을 검증한다.
"""
import sys
import os
import json
import functools
import inspect
from contextvars import ContextVar

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from whitelist import WHITELIST  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("system-mcp", stateless_http=True)

# 요청별 호출자 정보 (ASGI 미들웨어가 채운다)
_caller: ContextVar[dict] = ContextVar("caller", default={})


def get_caller() -> dict:
    return _caller.get() or {}


async def _log_execution(tool_name: str, params: dict, status: str, result):
    caller = get_caller()
    pool = await get_pool("system_db_dsn")
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
    """관리자 콘솔에서 토글한 활성/비활성 상태를 실행 시점에 확인한다."""
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow(
        "SELECT enabled FROM system_whitelist_state WHERE tool_name = $1", tool_name
    )
    if row is None:
        await pool.execute(
            "INSERT INTO system_whitelist_state (tool_name, enabled) VALUES ($1, $2) "
            "ON CONFLICT (tool_name) DO NOTHING",
            tool_name, default,
        )
        return default
    return row["enabled"]


def _check_roles(entry: dict) -> None:
    required = entry.get("required_roles")
    if not required:
        return
    roles = set(get_caller().get("roles", []))
    if not roles.intersection(set(required)):
        raise PermissionError(
            f"이 툴을 실행할 권한이 없습니다. 필요한 역할: {', '.join(required)}"
        )


def _make_wrapped_tool(name: str, entry: dict):
    """화이트리스트 항목에 권한 검사·감사로그를 덧씌우되, 원본 시그니처를 보존한다.
    functools.wraps가 __wrapped__를 설정하므로 inspect.signature가 원본 파라미터를
    그대로 읽고, FastMCP가 정확한 input schema를 생성한다."""
    handler = entry["handler"]

    @functools.wraps(handler)
    async def wrapped(*args, **kwargs):
        # 로그에는 위치인자도 이름으로 남긴다
        try:
            bound = inspect.signature(handler).bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
        except Exception:  # noqa: BLE001
            params = {"args": list(args), **kwargs}

        if not await _is_enabled(name, entry.get("enabled", False)):
            await _log_execution(name, params, "blocked", {"reason": "disabled by admin"})
            raise PermissionError(f"'{name}' 툴은 관리자 콘솔에서 비활성화되어 있습니다.")
        try:
            _check_roles(entry)
        except PermissionError as e:
            await _log_execution(name, params, "denied", {"reason": str(e)})
            raise

        try:
            result = await handler(*args, **kwargs)
            await _log_execution(name, params, "success", result)
            return result
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            await _log_execution(name, params, "error", {"error": str(e)})
            raise

    wrapped.__doc__ = entry["description"]
    return wrapped


for _name, _entry in WHITELIST.items():
    mcp.add_tool(_make_wrapped_tool(_name, _entry), name=_name, description=_entry["description"])


class CallerContextMiddleware:
    """Agent Server가 붙인 호출자 헤더를 ContextVar에 넣는 ASGI 미들웨어."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            roles = [r.strip() for r in headers.get("x-user-roles", "").split(",") if r.strip()]
            token = _caller.set({
                "user_id": headers.get("x-user-id"),
                "conversation_id": headers.get("x-conversation-id"),
                "request_id": headers.get("x-request-id"),
                "roles": roles,
            })
            try:
                await self.app(scope, receive, send)
            finally:
                _caller.reset(token)
            return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8004))
    app = CallerContextMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
