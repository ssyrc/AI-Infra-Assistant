"""
관리자 콘솔에서 특정 서비스 컨테이너를 재시작하는 기능(설정 저장/화이트리스트 변경 후 바로
반영하기 위함). Docker 소켓(/var/run/docker.sock)을 admin-console에 마운트해야 동작한다.

⚠️ 이 소켓 마운트는 admin-console 컨테이너에게 사실상 호스트 수준 권한을 준다(임의 컨테이너
실행/마운트가 가능해짐). 그 위험을 줄이려고 이 API 자체는 "정해진 서비스 이름 재시작"만
허용하고 임의 명령 실행 경로는 없다 — 그래도 admin-console 계정이 뚫리면 파급력이 커진다는
점은 그대로이니, 접근을 신뢰된 관리자망으로 제한하는 게 중요하다.
"""
import docker
from fastapi import APIRouter, Depends, HTTPException

from auth import require_admin

router = APIRouter(prefix="/api/ops", tags=["ops"])

ALLOWED_SERVICES = {"agent-server", "manual-mcp", "command-mcp", "voc-mcp", "system-mcp"}


@router.post("/restart/{service}")
async def restart_service(service: str, admin: str = Depends(require_admin)):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(400, f"재시작 가능한 서비스가 아닙니다: {service}")
    try:
        client = docker.from_env()
        matches = client.containers.list(all=True, filters={"name": service})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            500,
            f"docker 소켓에 접근할 수 없습니다({type(e).__name__}: {e}). "
            "docker-compose에 /var/run/docker.sock 마운트가 필요합니다.",
        )
    if not matches:
        raise HTTPException(404, f"'{service}' 컨테이너를 찾을 수 없습니다.")
    for c in matches:
        c.restart(timeout=15)
    return {"ok": True, "restarted": [c.name for c in matches]}
