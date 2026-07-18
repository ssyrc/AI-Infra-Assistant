"""
System MCP 화이트리스트.

- 여기 등록된 함수만 MCP 툴로 노출된다. 임의 커맨드/셸 실행 경로는 존재하지 않는다.
- 실제 백엔드 호출은 subprocess가 아니라 내부 REST API/CLI 래퍼로 구현하는 것을 권장한다
  (아래 예시는 s2 스케줄러 REST API를 호출하는 형태).
- 새 커맨드를 추가하려면 이 파일에 함수를 구현하고 WHITELIST에 등록한 뒤 재배포한다.
  (관리자 콘솔에서는 활성/비활성 토글만 가능하게 하고, 새 함수 자체는 코드 배포로만 추가한다.)
"""
import sys
import os
import httpx

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from config_store import get_config  # noqa: E402


async def get_scheduler_job_info(user_id: str) -> dict:
    """현재 사용자 '본인'의 스케줄러 job 정보를 조회한다.

    보안: user_id는 LLM/사용자가 정하는 값이 아니라, 호출자 신원(X-User-Id)에서
    서버가 강제 주입한다(user_scoped=True). 따라서 남의 user_id를 조회할 수 없다.
    """
    base_url = await get_config("scheduler_api_base_url")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base_url}/jobs", params={"user_id": user_id})
        resp.raise_for_status()
        return resp.json()


async def get_scheduler_queue_status() -> dict:
    """s2 스케줄러의 전체 큐 상태(대기/실행 중 job 수)를 조회한다."""
    base_url = await get_config("scheduler_api_base_url")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base_url}/queue/status")
        resp.raise_for_status()
        return resp.json()


# name -> 실행 핸들러와 메타데이터.
#  - enabled: 최초 기동 시 기본 활성 여부 (이후 관리자 콘솔 토글이 우선)
#  - required_roles: 지정하면 해당 역할을 가진 호출자만 실행 가능(빈 값이면 제한 없음).
#    Agent Server가 X-User-Roles 헤더로 전달한다. 콘솔에서 편집 가능(실행 시점 실시간 반영).
#  - user_scoped: True면 scope_param(기본 'user_id')을 LLM 스키마에서 감추고, 호출자
#    신원(X-User-Id)에서 강제 주입한다. 남의 자원을 조회/조작할 수 없게 하는 안전장치.
#    신뢰된 user_id가 없으면 실행을 거부한다(fail-closed).
#  - 상태를 바꾸는 툴을 추가할 때는 반드시 required_roles를 지정할 것.
WHITELIST = {
    "get_scheduler_job_info": {
        "handler": get_scheduler_job_info,
        "description": "현재 사용자 본인의 스케줄러 job 정보를 조회한다.",
        "params": {},              # user_id는 LLM에 노출하지 않고 호출자에서 주입
        "enabled": True,
        "required_roles": [],      # 본인 조회 전용 -> 역할 제한 없음
        "user_scoped": True,       # user_id를 호출자 신원에서 강제 주입
        "scope_param": "user_id",
    },
    "get_scheduler_queue_status": {
        "handler": get_scheduler_queue_status,
        "description": "s2 스케줄러 큐의 전체 대기/실행 상태를 조회한다(사용자별 데이터 아님).",
        "params": {},
        "enabled": True,
        "required_roles": [],
        "user_scoped": False,
    },
}
