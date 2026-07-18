"""
System MCP - 화이트리스트에 등록된 함수만 실행 가능한 MCP 서버.
whitelist.py에 없는 동작은 절대 수행하지 않는다 (임의 셸 실행 없음).
"""
import sys
import os
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from whitelist import WHITELIST  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("system-mcp", stateless_http=True)


async def _log_execution(tool_name: str, params: dict, status: str, result):
    pool = await get_pool("system_db_dsn")
    await pool.execute(
        """
        INSERT INTO job_logs (tool_name, params, requested_by, status, result)
        VALUES ($1, $2::jsonb, $3, $4, $5::jsonb)
        """,
        tool_name,
        json.dumps(params, ensure_ascii=False, default=str),
        "agent",  # 필요 시 세션에서 실제 사용자 ID를 전달받아 채운다
        status,
        json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
    )


async def _is_enabled(tool_name: str, default: bool) -> bool:
    """관리자 콘솔에서 토글한 활성/비활성 상태를 실행 시점에 확인한다.
    상태 행이 아직 없으면(최초 실행) 코드 기본값으로 생성해둔다."""
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow(
        "SELECT enabled FROM system_whitelist_state WHERE tool_name = $1", tool_name
    )
    if row is None:
        await pool.execute(
            """
            INSERT INTO system_whitelist_state (tool_name, enabled)
            VALUES ($1, $2)
            ON CONFLICT (tool_name) DO NOTHING
            """,
            tool_name,
            default,
        )
        return default
    return row["enabled"]


def _make_wrapped_tool(name: str, entry: dict):
    """화이트리스트 항목을 감사로그 + DB 활성상태 체크가 붙은 MCP 툴 함수로 감싼다."""
    handler = entry["handler"]

    async def wrapped(**kwargs):
        if not await _is_enabled(name, entry.get("enabled", False)):
            await _log_execution(name, kwargs, "blocked", {"reason": "disabled by admin"})
            raise PermissionError(f"'{name}' 툴은 관리자 콘솔에서 비활성화되어 있습니다.")
        try:
            result = await handler(**kwargs)
            await _log_execution(name, kwargs, "success", result)
            return result
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            await _log_execution(name, kwargs, "error", {"error": str(e)})
            raise

    wrapped.__name__ = name
    wrapped.__doc__ = entry["description"]
    return wrapped


# WHITELIST에 코드로 등록된 모든 항목을 MCP 툴로 노출한다.
# 실제 실행 가능 여부(on/off)는 매 호출 시 system_whitelist_state에서 확인한다
# -> 관리자 콘솔에서 끄면 MCP 서버 재배포 없이 즉시 차단된다.
for _name, _entry in WHITELIST.items():
    mcp.add_tool(_make_wrapped_tool(_name, _entry), name=_name, description=_entry["description"])


if __name__ == "__main__":
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8004)))
