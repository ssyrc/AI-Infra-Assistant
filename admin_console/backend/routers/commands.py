"""
Command MCP가 조회하는 커맨드 카탈로그(command_catalog) 관리 API.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from db import get_pool

router = APIRouter(prefix="/api/commands", tags=["commands"])


class CommandIn(BaseModel):
    name: str
    description: str
    usage: str | None = None
    category: str | None = None


@router.get("")
async def list_commands(admin: str = Depends(require_admin)):
    pool = await get_pool("command_db_dsn")
    rows = await pool.fetch(
        "SELECT id, name, description, usage, category, updated_at FROM command_catalog ORDER BY name"
    )
    return [dict(r) for r in rows]


@router.post("")
async def create_command(body: CommandIn, admin: str = Depends(require_admin)):
    pool = await get_pool("command_db_dsn")
    try:
        row_id = await pool.fetchval(
            """
            INSERT INTO command_catalog (name, description, usage, category)
            VALUES ($1, $2, $3, $4) RETURNING id
            """,
            body.name,
            body.description,
            body.usage,
            body.category,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"등록 실패 (이름 중복 가능): {e}")
    return {"id": row_id}


@router.patch("/{command_id}")
async def update_command(command_id: int, body: CommandIn, admin: str = Depends(require_admin)):
    pool = await get_pool("command_db_dsn")
    row = await pool.fetchrow(
        """
        UPDATE command_catalog SET name=$1, description=$2, usage=$3, category=$4, updated_at=now()
        WHERE id=$5 RETURNING id
        """,
        body.name,
        body.description,
        body.usage,
        body.category,
        command_id,
    )
    if not row:
        raise HTTPException(404, "커맨드를 찾을 수 없습니다.")
    return {"ok": True}


@router.delete("/{command_id}")
async def delete_command(command_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("command_db_dsn")
    await pool.execute("DELETE FROM command_catalog WHERE id = $1", command_id)
    return {"ok": True}
