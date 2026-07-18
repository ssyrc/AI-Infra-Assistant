"""
실행형 MCP(System/Command)가 공유하는 호출자 컨텍스트 + 안전 실행 래퍼.

- Agent Server가 붙인 호출자 헤더(X-User-Id/-Conversation-Id/-Request-Id/-User-Roles)를
  요청별 ContextVar에 담는다(CallerContextMiddleware).
- build_wrapped(): 화이트리스트 핸들러에 아래를 덧씌운다.
    · user_scoped 툴은 scope_param(기본 user_id)을 LLM 스키마에서 감추고 호출자 신원에서
      강제 주입한다. LLM/사용자가 준 값이 있어도 덮어쓰고, 신뢰된 id가 없으면 거부(fail-closed).
    · enabled/required_roles를 실행 시점에 DB에서 읽어 검사(콜백 주입).
    · 모든 실행을 감사 로그로 남긴다(콜백 주입).
  DB 접근(대상 DB/테이블)은 각 MCP가 콜백으로 넘겨, 이 모듈은 DB에 독립적이다.
- load_overrides_sync(): 기동 시 1회, 대상 DB에서 설명/역할 오버라이드를 읽는다.
"""
import os
import asyncio
import functools
import inspect
from contextvars import ContextVar

import asyncpg

_caller: ContextVar[dict] = ContextVar("caller", default={})


def get_caller() -> dict:
    return _caller.get() or {}


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


def load_overrides_sync(dsn_key: str, state_table: str) -> dict:
    """기동 시 1회, 대상 DB의 상태 테이블에서 설명/역할 오버라이드를 읽는다.
    공용 풀(get_pool)을 쓰면 임시 이벤트루프에 풀이 묶여 런타임에서 못 쓰므로 전용 연결을 쓴다."""
    async def _run() -> dict:
        config_dsn = os.environ.get("CONFIG_DB_DSN")
        if not config_dsn:
            return {}
        conn = await asyncpg.connect(config_dsn)
        try:
            dsn = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = $1", dsn_key)
        finally:
            await conn.close()
        if not dsn:
            return {}
        c2 = await asyncpg.connect(dsn)
        try:
            rows = await c2.fetch(
                f"SELECT tool_name, description_override, required_roles FROM {state_table}")
        finally:
            await c2.close()
        return {r["tool_name"]: dict(r) for r in rows}

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[mcp] 오버라이드 로드 실패, 코드 기본값 사용: {type(e).__name__}: {e}")
        return {}


def tool_description(name: str, entry: dict, overrides: dict) -> str:
    """LLM에 보일 설명: 콘솔 오버라이드가 있으면 그것을, 없으면 코드 설명을 쓴다."""
    ov = overrides.get(name, {})
    return (ov.get("description_override") or "").strip() or entry["description"]


def build_wrapped(name: str, entry: dict, *, is_enabled, required_roles, log_execution):
    """화이트리스트 항목에 권한 검사·감사로그·user_id 강제 주입을 덧씌운 async 함수를 만든다.

    is_enabled(name, default_bool) -> bool
    required_roles(name, code_default_list) -> list
    log_execution(name, params_dict, status_str, result) -> None
    """
    handler = entry["handler"]
    orig_sig = inspect.signature(handler)
    user_scoped = bool(entry.get("user_scoped", False))
    scope_param = entry.get("scope_param", "user_id")

    @functools.wraps(handler)
    async def wrapped(*args, **kwargs):
        if user_scoped:
            uid = get_caller().get("user_id")
            if not uid:
                # 신뢰된 호출자 신원이 없으면 실행하지 않는다(남의 자원 접근 방지, fail-closed).
                await log_execution(name, {}, "denied", {"reason": "no authenticated user_id"})
                raise PermissionError(
                    "호출자 사용자 식별자가 없어 실행할 수 없습니다. 관리자에게 문의하세요.")
            # LLM이 위치/키워드로 넣었을 수 있는 값을 무시하고 본인 id로 고정한다.
            args = ()
            kwargs[scope_param] = uid

        try:
            bound = orig_sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
        except Exception:  # noqa: BLE001
            params = {"args": list(args), **kwargs}

        if not await is_enabled(name, entry.get("enabled", False)):
            await log_execution(name, params, "blocked", {"reason": "disabled by admin"})
            raise PermissionError(f"'{name}' 툴은 관리자 콘솔에서 비활성화되어 있습니다.")

        required = await required_roles(name, entry.get("required_roles") or [])
        if required:
            roles = set(get_caller().get("roles", []))
            if not roles.intersection(set(required)):
                msg = f"필요한 역할: {', '.join(required)}"
                await log_execution(name, params, "denied", {"reason": msg})
                raise PermissionError("이 툴을 실행할 권한이 없습니다. " + msg)

        try:
            result = await handler(*args, **kwargs)
            await log_execution(name, params, "success", result)
            return result
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            await log_execution(name, params, "error", {"error": str(e)})
            raise

    # user_scoped 주입 파라미터는 LLM 입력 스키마(시그니처+어노테이션)에서 제거한다.
    if user_scoped:
        reduced = [p for pn, p in orig_sig.parameters.items() if pn != scope_param]
        wrapped.__signature__ = orig_sig.replace(parameters=reduced)
        wrapped.__annotations__ = {
            k: v for k, v in getattr(handler, "__annotations__", {}).items() if k != scope_param
        }
    return wrapped
