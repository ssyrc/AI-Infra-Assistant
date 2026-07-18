"""
플랫폼 전역 설정(vLLM 주소, 각 MCP DB DSN, MCP 엔드포인트, 에이전트 시스템 지시문) 관리 API.
실제 값은 shared/config_store.py를 통해 platform_config DB의 platform_settings 테이블에 저장된다.
"""
import os
import sys

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import require_admin

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../shared"))
from config_store import list_config, set_config  # noqa: E402

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingIn(BaseModel):
    value: str


@router.get("")
async def get_settings(admin: str = Depends(require_admin)):
    rows = await list_config()
    for r in rows:
        if r["is_secret"] and r["value"]:
            r["value"] = "•" * 8 + r["value"][-4:] if len(r["value"]) > 4 else "••••"
    return rows


@router.put("/{key}")
async def update_setting(key: str, body: SettingIn, admin: str = Depends(require_admin)):
    await set_config(key, body.value, updated_by=admin)
    return {"ok": True}
