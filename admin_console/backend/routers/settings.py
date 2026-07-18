"""
플랫폼 전역 설정(vLLM 주소, 각 MCP DB DSN, MCP 엔드포인트, 에이전트 시스템 지시문) 관리 API.
실제 값은 shared/config_store.py를 통해 platform_config DB의 platform_settings 테이블에 저장된다.
"""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../shared"))
from config_store import list_config, set_config  # noqa: E402

router = APIRouter(prefix="/api/settings", tags=["settings"])

# .env(환경변수)에서 매 기동 시 재주입되는(force=True) 키들.
# 콘솔에서 고쳐도 재시작하면 .env 값으로 덮어써지므로, UI에서 읽기 전용으로 표시하고
# 저장 요청도 막는다(무의미한 편집로 인한 혼란 방지). 값 변경은 .env에서 한다.
# (shared/migrations.py::config_seed의 force=True 항목과 일치해야 한다.)
ENV_MANAGED_KEYS = {
    "manual_db_dsn", "voc_db_dsn", "command_db_dsn", "system_db_dsn",
    "agent_session_db_dsn", "redis_url",
}


class SettingIn(BaseModel):
    value: str


@router.get("")
async def get_settings(admin: str = Depends(require_admin)):
    rows = await list_config()
    for r in rows:
        r["env_managed"] = r["key"] in ENV_MANAGED_KEYS
        if r["is_secret"] and r["value"]:
            r["value"] = "•" * 8 + r["value"][-4:] if len(r["value"]) > 4 else "••••"
    return rows


@router.put("/{key}")
async def update_setting(key: str, body: SettingIn, admin: str = Depends(require_admin)):
    if key in ENV_MANAGED_KEYS:
        raise HTTPException(
            400, "이 값은 .env(환경변수)로 관리됩니다. .env를 수정하고 해당 서비스를 재시작하세요.")
    await set_config(key, body.value, updated_by=admin)
    return {"ok": True}
