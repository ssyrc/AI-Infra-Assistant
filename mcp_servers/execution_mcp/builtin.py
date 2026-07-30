"""
Execution MCP의 **코드 내장 커맨드** - 지정 서버에 ssh(root)로 접속해 호출자(user_id) 권한으로
실행하는 read-only 리눅스 명령.

콘솔에 등록하는 커맨드(execution_commands)와 달리 여기 있는 것들은 **파이썬 함수**다.
그 이유는 값 검증 때문이다 - `lines`는 1~2000, `max_depth`는 0~10, `kind`는 정해진 다섯 개,
경로는 safe_path()를 통과해야 한다. 템플릿 문자열로는 표현할 수 없는 검사라 코드로 남긴다.
활성/역할/설명/실행위치는 콘솔에서 바꾼다(execution_builtin_state).

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


# 툴 이름 -> 실행 핸들러와 메타데이터.
#  - enabled: 최초 기동 시 기본 활성 여부(이후 관리자 콘솔 토글이 우선). 전부 read-only라 기본 ON.
#  - required_roles: 지정 시 해당 역할 보유자만 실행(콘솔 편집, 실시간). X-User-Roles로 검증.
#  - user_scoped: True면 user_id를 LLM 스키마에서 감추고 호출자 신원에서 강제 주입.
#  - host_mode: host 파라미터 처리 방식(관리자 콘솔에서 변경 가능, 단 스키마에
#    영향을 주므로 반영에는 Execution MCP 재시작이 필요함).
#      target_server(기본) - 서버마다 값이 다른 툴. host를 LLM이 지정해야 한다(예: hgpu8002의
#        GPU 상태는 hgpu8002에서만 의미가 있음).
#      login_server - 특정 서버에 매인 게 아니라 로그인 서버 기준으로 보는 게 자연스러운 툴.
#        host를 LLM 스키마에서 아예 숨기고 scheduler_login_host로 자동 고정한다.
_COMMON = {"enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
           "host_mode": "target_server"}
_LOGIN_SERVER = {**_COMMON, "host_mode": "login_server"}

BUILTIN_COMMANDS = {
    "gpu_status": {"handler": gpu_status, "example_command": "nvidia-smi",
                   "description": "지정한 서버의 GPU 상태. 장수·모델·사용률·메모리·실행 중인 프로세스",
                   **_COMMON},
    "list_dir": {"handler": list_dir, "example_command": "ls -lh [-a] <path>",
                 "description": "내 홈이나 지정한 경로의 파일 목록", **_LOGIN_SERVER},
    "find_files": {"handler": find_files, "example_command": "find <path> [-name pattern] [-type f|d|l] (read-only)",
                   "description": "이름 패턴이나 종류로 파일 찾기. 읽기 전용", **_LOGIN_SERVER},
    "disk_free": {"handler": disk_free, "example_command": "df -h",
                  "description": "서버 파일시스템의 디스크 여유 용량. "
                                  "개인 홈 할당량 조회에는 쓰지 않는다",
                  **_COMMON},
    "disk_usage": {"handler": disk_usage, "example_command": "du -h --max-depth=<n> <path>",
                   "description": "지정한 경로가 디스크를 얼마나 쓰는지", **_COMMON},
    "read_file_head": {"handler": read_file_head, "example_command": "head -n <lines> <path>",
                       "description": "텍스트 파일 앞부분 읽기. 로그·설정 확인용", **_LOGIN_SERVER},
    "system_info": {"handler": system_info,
                    "example_command": "uptime | free -h | ip addr | who | lscpu (kind로 선택)",
                    "description": "서버 시스템 정보. kind로 uptime, memory, network, who, cpu 중 선택", **_COMMON},
}
