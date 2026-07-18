"""
System MCP 화이트리스트 on/off 토글 + job_logs(실행 감사로그) 조회 API.
새 화이트리스트 함수 자체의 '구현'은 이 콘솔에서 만들 수 없다 (System MCP 코드 배포 필요).
여기서는 이미 배포된 항목의 활성/비활성만 제어한다.
"""
import sys
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from db import get_pool

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/system_mcp"))
from whitelist import WHITELIST  # noqa: E402  (핸들러 함수 자체가 아니라 name/description 메타데이터만 사용)

router = APIRouter(prefix="/api/system", tags=["system"])


class WhitelistPatchIn(BaseModel):
    """부분 수정. 제공된 필드만 반영한다.
    - enabled: 활성/비활성 (실시간)
    - required_roles: 필요 역할 목록 (실시간). 빈 목록이면 역할 제한 없음.
    - description: LLM에 보일 설명 오버라이드. 빈 문자열이면 오버라이드 해제(코드 설명 사용).
      설명 변경은 System MCP 재시작 후 에이전트에 반영된다.
    """
    enabled: bool | None = None
    required_roles: list[str] | None = None
    description: str | None = None


@router.get("/whitelist")
async def list_whitelist(admin: str = Depends(require_admin)):
    """System MCP 코드에 등록된 모든 항목을, 아직 한 번도 실행되지 않아 DB 상태 행이
    없는 것까지 포함해서 보여준다. 설명/역할은 콘솔 오버라이드가 있으면 그 값을 보여준다."""
    pool = await get_pool("system_db_dsn")
    db_rows = {
        r["tool_name"]: r
        for r in await pool.fetch(
            "SELECT tool_name, enabled, required_roles, description_override, updated_by, updated_at "
            "FROM system_whitelist_state"
        )
    }
    result = []
    for name, entry in WHITELIST.items():
        db_row = db_rows.get(name)
        override = db_row["description_override"] if db_row else None
        if db_row and db_row["required_roles"] is not None:
            roles = list(db_row["required_roles"])
        else:
            roles = list(entry.get("required_roles") or [])
        result.append(
            {
                "tool_name": name,
                "description": (override or "").strip() or entry["description"],
                "code_description": entry["description"],
                "description_override": override,
                "required_roles": roles,
                "user_scoped": bool(entry.get("user_scoped", False)),
                "enabled": db_row["enabled"] if db_row else entry.get("enabled", False),
                "updated_by": db_row["updated_by"] if db_row else None,
                "updated_at": db_row["updated_at"] if db_row else None,
            }
        )
    return result


@router.patch("/whitelist/{tool_name}")
async def patch_whitelist(tool_name: str, body: WhitelistPatchIn, admin: str = Depends(require_admin)):
    if tool_name not in WHITELIST:
        raise HTTPException(404, "알 수 없는 화이트리스트 항목입니다(코드에 없는 툴).")
    code = WHITELIST[tool_name]
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow(
        "SELECT enabled, required_roles, description_override FROM system_whitelist_state WHERE tool_name = $1",
        tool_name,
    )

    enabled = body.enabled if body.enabled is not None else (
        row["enabled"] if row else code.get("enabled", False))

    if body.required_roles is not None:
        roles = [r.strip() for r in body.required_roles if r and r.strip()]
    elif row and row["required_roles"] is not None:
        roles = list(row["required_roles"])
    else:
        roles = list(code.get("required_roles") or [])

    if body.description is not None:
        desc = body.description.strip() or None   # 빈 문자열 -> 오버라이드 해제
    else:
        desc = row["description_override"] if row else None

    await pool.execute(
        """
        INSERT INTO system_whitelist_state (tool_name, enabled, required_roles, description_override, updated_by, updated_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (tool_name)
        DO UPDATE SET enabled = $2, required_roles = $3, description_override = $4,
                      updated_by = $5, updated_at = now()
        """,
        tool_name, enabled, roles, desc, admin,
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
