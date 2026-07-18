"""
System MCP - 화이트리스트에 등록된 함수만 실행 가능한 MCP 서버.
whitelist.py에 없는 동작은 절대 수행하지 않는다 (임의 셸 실행 없음).

보안 설계:
- functools.wraps로 원본 함수 시그니처를 보존한다. 이렇게 해야 MCP input schema에
  실제 파라미터가 정확히 노출되어 LLM이 올바르게 호출할 수 있다.
- user_scoped 항목은 scope_param(기본 user_id)을 LLM 스키마에서 '감추고', 호출자
  신원(X-User-Id)에서 강제 주입한다. LLM/사용자가 준 값이 있어도 덮어써 본인으로 고정하며,
  신뢰된 user_id가 없으면 실행을 거부한다(fail-closed). -> 남의 자원을 볼 수 없다.
- 호출자 정보(사용자 ID/대화 ID/요청 ID/역할)는 HTTP 헤더로 전달받아 감사로그·권한검사에 쓴다.
  Agent Server가 X-User-Id / X-Conversation-Id / X-Request-Id / X-User-Roles 헤더를 붙여준다.
- enabled(활성)와 required_roles(필요 역할)는 관리자 콘솔에서 편집하며 '실행 시점'에 DB에서
  실시간으로 읽는다. description_override(LLM에 보이는 설명)는 기동 시 1회 읽어 반영한다
  (설명 변경은 이 MCP 재시작이 필요 — 기존 hot_reload=false 정책과 동일).
"""
import sys
import os
import json
import asyncio
import functools
import inspect
from contextvars import ContextVar

import asyncpg

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from whitelist import WHITELIST  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("system-mcp", stateless_http=True)

# 요청별 호출자 정보 (ASGI 미들웨어가 채운다)
_caller: ContextVar[dict] = ContextVar("caller", default={})


def get_caller() -> dict:
    return _caller.get() or {}


def _load_overrides_sync() -> dict:
    """기동 시 1회, DB에서 화이트리스트 오버라이드(설명/역할)를 읽어 온다.
    공용 get_pool을 쓰면 여기서 만든 임시 이벤트루프에 풀이 묶여 런타임에서 재사용할 수 없으므로,
    전용 asyncpg 연결을 열고 바로 닫는다(풀 캐시를 오염시키지 않는다)."""
    async def _run() -> dict:
        config_dsn = os.environ.get("CONFIG_DB_DSN")
        if not config_dsn:
            return {}
        conn = await asyncpg.connect(config_dsn)
        try:
            sys_dsn = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = 'system_db_dsn'")
        finally:
            await conn.close()
        if not sys_dsn:
            return {}
        c2 = await asyncpg.connect(sys_dsn)
        try:
            rows = await c2.fetch(
                "SELECT tool_name, description_override, required_roles FROM system_whitelist_state")
        finally:
            await c2.close()
        return {r["tool_name"]: dict(r) for r in rows}

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[system-mcp] 오버라이드 로드 실패, 코드 기본값 사용: {type(e).__name__}: {e}")
        return {}


_OVERRIDES = _load_overrides_sync()


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


async def _required_roles(tool_name: str, code_default: list) -> list:
    """필요 역할을 실행 시점에 DB에서 읽는다(콘솔 편집이 즉시 반영). 행이 없으면 코드 기본값."""
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow(
        "SELECT required_roles FROM system_whitelist_state WHERE tool_name = $1", tool_name
    )
    if row and row["required_roles"] is not None:
        return list(row["required_roles"])
    return list(code_default or [])


async def _check_roles(tool_name: str, entry: dict) -> None:
    required = await _required_roles(tool_name, entry.get("required_roles") or [])
    if not required:
        return
    roles = set(get_caller().get("roles", []))
    if not roles.intersection(set(required)):
        raise PermissionError(
            f"이 툴을 실행할 권한이 없습니다. 필요한 역할: {', '.join(required)}"
        )


def _make_wrapped_tool(name: str, entry: dict):
    """화이트리스트 항목에 권한 검사·감사로그·user_id 강제 주입을 덧씌운다.
    user_scoped 항목은 scope_param을 LLM 스키마에서 제거하고 호출자 신원에서 주입한다."""
    handler = entry["handler"]
    orig_sig = inspect.signature(handler)
    user_scoped = bool(entry.get("user_scoped", False))
    scope_param = entry.get("scope_param", "user_id")

    @functools.wraps(handler)
    async def wrapped(*args, **kwargs):
        if user_scoped:
            uid = get_caller().get("user_id")
            if not uid:
                # 신뢰된 호출자 신원이 없으면 실행하지 않는다(남의 자원 조회 방지, fail-closed).
                await _log_execution(name, {}, "denied", {"reason": "no authenticated user_id"})
                raise PermissionError(
                    "호출자 사용자 식별자가 없어 실행할 수 없습니다. 관리자에게 문의하세요."
                )
            # LLM이 위치/키워드로 넣었을 수 있는 값을 무시하고 본인 id로 고정한다.
            args = ()
            kwargs[scope_param] = uid

        try:
            bound = orig_sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
        except Exception:  # noqa: BLE001
            params = {"args": list(args), **kwargs}

        if not await _is_enabled(name, entry.get("enabled", False)):
            await _log_execution(name, params, "blocked", {"reason": "disabled by admin"})
            raise PermissionError(f"'{name}' 툴은 관리자 콘솔에서 비활성화되어 있습니다.")
        try:
            await _check_roles(name, entry)
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

    # user_scoped 주입 파라미터는 LLM에 노출할 시그니처(=input schema)에서 제거한다.
    # 시그니처와 어노테이션 양쪽에서 지워, FastMCP가 어느 쪽을 읽어도 노출되지 않게 한다.
    # (어노테이션은 핸들러 원본을 건드리지 않도록 필터링한 새 dict로 교체한다.)
    if user_scoped:
        reduced = [p for pn, p in orig_sig.parameters.items() if pn != scope_param]
        wrapped.__signature__ = orig_sig.replace(parameters=reduced)
        wrapped.__annotations__ = {
            k: v for k, v in getattr(handler, "__annotations__", {}).items() if k != scope_param
        }

    wrapped.__doc__ = _tool_description(name, entry)
    return wrapped


def _tool_description(name: str, entry: dict) -> str:
    """LLM에 보일 설명: 콘솔에서 저장한 오버라이드가 있으면 그것을, 없으면 코드 설명을 쓴다."""
    ov = _OVERRIDES.get(name, {})
    return (ov.get("description_override") or "").strip() or entry["description"]


for _name, _entry in WHITELIST.items():
    mcp.add_tool(_make_wrapped_tool(_name, _entry), name=_name,
                 description=_tool_description(_name, _entry))


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
