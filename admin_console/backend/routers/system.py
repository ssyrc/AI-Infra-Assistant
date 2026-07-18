"""
System MCP 화이트리스트 on/off 토글 + job_logs(실행 감사로그) 조회 API.
새 화이트리스트 함수 자체의 '구현'은 이 콘솔에서 만들 수 없다 (System MCP 코드 배포 필요).
여기서는 이미 배포된 항목의 활성/비활성만 제어한다.
"""
import sys
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import require_admin
from db import get_pool

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/system_mcp"))
from whitelist import WHITELIST  # noqa: E402  (핸들러 함수 자체가 아니라 name/description 메타데이터만 사용)

router = APIRouter(prefix="/api/system", tags=["system"])


class ToggleIn(BaseModel):
    enabled: bool


@router.get("/whitelist")
async def list_whitelist(admin: str = Depends(require_admin)):
    """System MCP 코드에 등록된 모든 항목을, 아직 한 번도 실행되지 않아 DB 상태 행이
    없는 것까지 포함해서 보여준다."""
    pool = await get_pool("system_db_dsn")
    db_rows = {
        r["tool_name"]: r
        for r in await pool.fetch(
            "SELECT tool_name, enabled, updated_by, updated_at FROM system_whitelist_state"
        )
    }
    result = []
    for name, entry in WHITELIST.items():
        db_row = db_rows.get(name)
        result.append(
            {
                "tool_name": name,
                "description": entry["description"],
                "enabled": db_row["enabled"] if db_row else entry.get("enabled", False),
                "updated_by": db_row["updated_by"] if db_row else None,
                "updated_at": db_row["updated_at"] if db_row else None,
            }
        )
    return result


@router.patch("/whitelist/{tool_name}")
async def toggle_whitelist(tool_name: str, body: ToggleIn, admin: str = Depends(require_admin)):
    pool = await get_pool("system_db_dsn")
    await pool.execute(
        """
        INSERT INTO system_whitelist_state (tool_name, enabled, updated_by, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (tool_name)
        DO UPDATE SET enabled = $2, updated_by = $3, updated_at = now()
        """,
        tool_name,
        body.enabled,
        admin,
    )
    return {"ok": True}


@router.get("/logs")
async def list_logs(limit: int = 100, admin: str = Depends(require_admin)):
    pool = await get_pool("system_db_dsn")
    rows = await pool.fetch(
        """
        SELECT id, tool_name, params, requested_by, status, result, created_at
        FROM job_logs ORDER BY created_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]
