"""
System MCP 화이트리스트 on/off 토글 + job_logs(실행 감사로그) 조회 API.
코드 내장 화이트리스트 함수 자체의 '구현'은 이 콘솔에서 만들 수 없다 (System MCP 코드 배포 필요).
대신 /custom-commands로 콘솔에서 직접 새 커맨드(argv 기반)를 등록할 수 있다 - 그것도 System MCP
기동 시 1회만 반영되므로 추가/수정 후에는 System MCP 재시작이 필요하다.
"""
import json
import sys
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from db import get_pool

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/system_mcp"))
from whitelist import WHITELIST  # noqa: E402  (핸들러 함수 자체가 아니라 name/description 메타데이터만 사용)

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../shared"))
from custom_commands import validate_definition  # noqa: E402

router = APIRouter(prefix="/api/system", tags=["system"])

HOST_MODES = {"target_server", "login_server"}


class WhitelistPatchIn(BaseModel):
    """부분 수정. 제공된 필드만 반영한다.
    - enabled: 활성/비활성 (실시간)
    - required_roles: 필요 역할 목록 (실시간). 빈 목록이면 역할 제한 없음.
    - description: LLM에 보일 설명 오버라이드. 빈 문자열이면 오버라이드 해제(코드 설명 사용).
      설명 변경은 System MCP 재시작 후 에이전트에 반영된다.
    - host_mode: "target_server"(LLM이 서버명을 지정) | "login_server"(host를 LLM 스키마에서
      숨기고 로그인 서버로 고정). LLM에 노출되는 파라미터가 바뀌므로 System MCP 재시작 필요.
    """
    enabled: bool | None = None
    required_roles: list[str] | None = None
    description: str | None = None
    host_mode: str | None = None


@router.get("/whitelist")
async def list_whitelist(admin: str = Depends(require_admin)):
    """System MCP 코드에 등록된 모든 항목을, 아직 한 번도 실행되지 않아 DB 상태 행이
    없는 것까지 포함해서 보여준다. 설명/역할은 콘솔 오버라이드가 있으면 그 값을 보여준다."""
    pool = await get_pool("system_db_dsn")
    db_rows = {
        r["tool_name"]: r
        for r in await pool.fetch(
            "SELECT tool_name, enabled, required_roles, description_override, host_mode, "
            "updated_by, updated_at FROM system_whitelist_state"
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
                "example_command": entry.get("example_command", ""),
                "required_roles": roles,
                "user_scoped": bool(entry.get("user_scoped", False)),
                "custom": False,
                "enabled": db_row["enabled"] if db_row else entry.get("enabled", False),
                "host_mode": (db_row["host_mode"] if db_row else None) or entry.get("host_mode", "target_server"),
                "updated_by": db_row["updated_by"] if db_row else None,
                "updated_at": db_row["updated_at"] if db_row else None,
            }
        )
    return result


@router.patch("/whitelist/{tool_name}")
async def patch_whitelist(tool_name: str, body: WhitelistPatchIn, admin: str = Depends(require_admin)):
    if tool_name not in WHITELIST:
        raise HTTPException(404, "알 수 없는 화이트리스트 항목입니다(코드에 없는 툴).")
    if body.host_mode is not None and body.host_mode not in HOST_MODES:
        raise HTTPException(400, f"host_mode는 {', '.join(sorted(HOST_MODES))} 중 하나여야 합니다.")
    code = WHITELIST[tool_name]
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow(
        "SELECT enabled, required_roles, description_override, host_mode "
        "FROM system_whitelist_state WHERE tool_name = $1",
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

    host_mode = body.host_mode or (row["host_mode"] if row else None) or code.get("host_mode", "target_server")

    await pool.execute(
        """
        INSERT INTO system_whitelist_state
            (tool_name, enabled, required_roles, description_override, host_mode, updated_by, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (tool_name)
        DO UPDATE SET enabled = $2, required_roles = $3, description_override = $4,
                      host_mode = $5, updated_by = $6, updated_at = now()
        """,
        tool_name, enabled, roles, desc, host_mode, admin,
    )
    return {"ok": True, "restart_required": body.host_mode is not None}


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


class CommandParamIn(BaseModel):
    name: str
    type: str = "str"


class CustomCommandIn(BaseModel):
    """argv_template: 커맨드 토큰 리스트(예: ["iostat", "-x", "1", "{count}"]).
    "{param}" 형태 토큰만 params에 정의된 값으로 치환되고, 나머지는 그대로 실행된다(셸 미사용).
    host/user_id는 예약되어 있다 - 항상 자동으로 붙는다(호출자 본인 권한으로만 실행).
    host_mode: "target_server"(기본, LLM이 서버명을 지정) | "login_server"(host를 LLM 스키마에서
    숨기고 로그인 서버로 고정 실행)."""
    tool_name: str
    description: str
    argv_template: list[str]
    params: list[CommandParamIn] = []
    required_roles: list[str] = []
    enabled: bool = False
    host_mode: str = "target_server"


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["argv_template"] = json.loads(d["argv_template"])
    d["params"] = json.loads(d["params"])
    return d


@router.get("/custom-commands")
async def list_custom_commands(admin: str = Depends(require_admin)):
    pool = await get_pool("system_db_dsn")
    rows = await pool.fetch(
        "SELECT tool_name, description, argv_template, params, required_roles, enabled, host_mode, "
        "created_by, created_at, updated_by, updated_at FROM system_custom_commands "
        "ORDER BY created_at DESC"
    )
    return [_row_to_dict(r) for r in rows]


@router.post("/custom-commands")
async def create_custom_command(body: CustomCommandIn, admin: str = Depends(require_admin)):
    if body.host_mode not in HOST_MODES:
        raise HTTPException(400, f"host_mode는 {', '.join(sorted(HOST_MODES))} 중 하나여야 합니다.")
    pool = await get_pool("system_db_dsn")
    existing = await pool.fetch("SELECT tool_name FROM system_custom_commands")
    existing_names = {r["tool_name"] for r in existing} | set(WHITELIST.keys())
    params = [p.model_dump() for p in body.params]
    try:
        validate_definition(body.tool_name, body.argv_template, params, existing_names)
    except ValueError as e:
        raise HTTPException(400, str(e))

    await pool.execute(
        """
        INSERT INTO system_custom_commands
            (tool_name, description, argv_template, params, required_roles, enabled, host_mode,
             created_by, updated_by)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $8)
        """,
        body.tool_name, body.description, json.dumps(body.argv_template), json.dumps(params),
        [r.strip() for r in body.required_roles if r and r.strip()], body.enabled, body.host_mode, admin,
    )
    return {"ok": True, "restart_required": True}


@router.patch("/custom-commands/{tool_name}")
async def update_custom_command(tool_name: str, body: CustomCommandIn, admin: str = Depends(require_admin)):
    if body.host_mode not in HOST_MODES:
        raise HTTPException(400, f"host_mode는 {', '.join(sorted(HOST_MODES))} 중 하나여야 합니다.")
    pool = await get_pool("system_db_dsn")
    row = await pool.fetchrow("SELECT tool_name FROM system_custom_commands WHERE tool_name = $1", tool_name)
    if not row:
        raise HTTPException(404, "등록되지 않은 커맨드입니다.")
    if body.tool_name != tool_name:
        raise HTTPException(400, "이름은 바꿀 수 없습니다(삭제 후 새로 등록하세요).")
    params = [p.model_dump() for p in body.params]
    try:
        validate_definition(tool_name, body.argv_template, params, set(WHITELIST.keys()))
    except ValueError as e:
        raise HTTPException(400, str(e))

    await pool.execute(
        """
        UPDATE system_custom_commands
        SET description = $2, argv_template = $3::jsonb, params = $4::jsonb,
            required_roles = $5, enabled = $6, host_mode = $7, updated_by = $8, updated_at = now()
        WHERE tool_name = $1
        """,
        tool_name, body.description, json.dumps(body.argv_template), json.dumps(params),
        [r.strip() for r in body.required_roles if r and r.strip()], body.enabled, body.host_mode, admin,
    )
    return {"ok": True, "restart_required": True}


@router.delete("/custom-commands/{tool_name}")
async def delete_custom_command(tool_name: str, admin: str = Depends(require_admin)):
    pool = await get_pool("system_db_dsn")
    await pool.execute("DELETE FROM system_custom_commands WHERE tool_name = $1", tool_name)
    return {"ok": True, "restart_required": True}
