"""
System MCP 화이트리스트 - 사용자가 지정한 '서버'에 ssh(root)로 접속해 호출자(user_id)
권한으로 실행하는 read-only 리눅스 명령.

동작:
- LLM은 서버 이름(host)과 타입이 정해진 파라미터만 준다. 원시 셸/플래그는 노출하지 않는다.
- host는 /etc/hosts에 등록된 서버만 허용된다(ssh_exec.resolve_host = 화이트리스트).
- 모든 툴은 user_scoped=True: user_id는 LLM 스키마에서 감추고 호출자 신원에서 강제 주입한다.
  ssh root@host 후 `su - user_id`로 강등해 실행하므로 남의 권한으로 실행할 수 없다.
- 기본 enabled=False: ssh 키/‏/etc/hosts 마운트 등 인프라가 준비된 뒤 관리자 콘솔에서 켠다.
  rm 등 파괴적 명령은 아예 등록하지 않는다.

예) 사용자가 "hgpu8002 서버에 GPU가 이상해요" -> gpu_status(host='hgpu8002')
    -> /etc/hosts에서 hgpu8002의 IP 조회 -> ssh root -> su - <user> -> nvidia-smi -> 결과 판단.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from ssh_exec import run_ssh_as_user  # noqa: E402
from linux_exec import safe_path, build_find_argv  # noqa: E402


async def gpu_status(user_id: str, host: str) -> dict:
    """지정 서버의 GPU 상태를 조회한다(nvidia-smi). 'GPU가 이상하다/몇 장 인식되나' 확인용."""
    return await run_ssh_as_user(host, user_id, ["nvidia-smi"])


async def list_dir(user_id: str, host: str, path: str = ".", show_hidden: bool = False) -> dict:
    """지정 서버의 디렉토리 내용을 나열한다(ls). show_hidden=True면 숨김 파일 포함(-a)."""
    argv = ["ls", "-lh", "-a" if show_hidden else "-A", "--", safe_path(path)]
    return await run_ssh_as_user(host, user_id, argv)


async def find_files(user_id: str, host: str, path: str = ".", name_pattern: str | None = None,
                     type: str | None = None, max_depth: int | None = None) -> dict:
    """지정 서버에서 파일을 검색한다(find, 읽기 전용). name_pattern은 glob(예: '*.log'),
    type은 f/d/l, max_depth로 깊이 제한. -exec/-delete 등은 지원하지 않는다."""
    return await run_ssh_as_user(host, user_id, build_find_argv(path, name_pattern, type, max_depth))


async def disk_free(user_id: str, host: str) -> dict:
    """지정 서버의 파일시스템별 디스크 여유/사용 용량을 조회한다(df -h)."""
    return await run_ssh_as_user(host, user_id, ["df", "-h"])


async def disk_usage(user_id: str, host: str, path: str = ".", max_depth: int = 1) -> dict:
    """지정 서버에서 경로의 디스크 사용량을 조회한다(du). max_depth로 하위 깊이를 제한한다."""
    md = int(max_depth)
    if md < 0 or md > 10:
        raise ValueError("max_depth는 0~10 사이여야 합니다.")
    return await run_ssh_as_user(host, user_id, ["du", "-h", f"--max-depth={md}", "--", safe_path(path)])


async def read_file_head(user_id: str, host: str, path: str, lines: int = 200) -> dict:
    """지정 서버에서 텍스트 파일 앞부분을 읽는다(head). 호출자 권한으로만 읽는다."""
    n = int(lines)
    if n < 1 or n > 2000:
        raise ValueError("lines는 1~2000 사이여야 합니다.")
    return await run_ssh_as_user(host, user_id, ["head", "-n", str(n), "--", safe_path(path)])


async def system_info(user_id: str, host: str, kind: str = "uptime") -> dict:
    """지정 서버의 시스템 정보를 조회한다. kind: uptime|memory|network|who|cpu."""
    table = {
        "uptime": ["uptime"],
        "memory": ["free", "-h"],
        "network": ["ip", "addr"],
        "who": ["who"],
        "cpu": ["lscpu"],
    }
    if kind not in table:
        raise ValueError("kind는 uptime|memory|network|who|cpu 중 하나입니다.")
    return await run_ssh_as_user(host, user_id, table[kind])


# name -> 실행 핸들러와 메타데이터.
#  - enabled: 최초 기동 시 기본 활성 여부(이후 관리자 콘솔 토글이 우선). ssh 실행은 기본 OFF.
#  - required_roles: 지정 시 해당 역할 보유자만 실행(콘솔 편집, 실시간). X-User-Roles로 검증.
#  - user_scoped: True면 user_id를 LLM 스키마에서 감추고 호출자 신원에서 강제 주입.
_COMMON = {"enabled": False, "required_roles": [], "user_scoped": True, "scope_param": "user_id"}

WHITELIST = {
    "gpu_status": {"handler": gpu_status,
                   "description": ("지정 서버(host)의 GPU 상태를 조회한다(nvidia-smi). "
                                   "'특정 서버 GPU가 이상하다/몇 장 인식되나'를 확인할 때 사용. "
                                   "host는 서버 이름(예: hgpu8002)."), **_COMMON},
    "list_dir": {"handler": list_dir,
                 "description": "지정 서버(host)에서 본인 권한으로 디렉토리를 나열한다(ls).", **_COMMON},
    "find_files": {"handler": find_files,
                   "description": ("지정 서버(host)에서 파일을 검색한다(find, 읽기 전용). "
                                   "이름 패턴/종류로 찾는다. 수정·삭제·실행은 못 한다."), **_COMMON},
    "disk_free": {"handler": disk_free,
                  "description": "지정 서버(host)의 디스크 여유/사용 용량을 조회한다(df -h).", **_COMMON},
    "disk_usage": {"handler": disk_usage,
                   "description": "지정 서버(host)에서 경로의 디스크 사용량을 조회한다(du).", **_COMMON},
    "read_file_head": {"handler": read_file_head,
                       "description": "지정 서버(host)에서 텍스트 파일 앞부분을 읽는다(head).", **_COMMON},
    "system_info": {"handler": system_info,
                    "description": ("지정 서버(host)의 시스템 정보를 조회한다: "
                                    "uptime/memory/network/who/cpu."), **_COMMON},
}
