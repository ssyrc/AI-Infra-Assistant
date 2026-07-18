"""
System MCP 화이트리스트 - 호출자(user_id) 권한으로 실행하는 read-only 리눅스 명령.

설계:
- 각 툴은 원시 셸/플래그를 LLM에 노출하지 않고, 타입이 정해진 파라미터만 받는다. 내부에서
  argv를 만들어 linux_exec.run_as_user로 실행하므로(셸 없음) 명령 주입이 불가능하다.
- 모든 툴은 user_scoped=True: user_id를 LLM 스키마에서 감추고 호출자 신원에서 주입해,
  호출자 '본인 권한'으로만 실행한다(남의 파일을 그의 권한으로 못 본다).
- 기본 enabled=False: root 권한/호스트 사용자 계정 등 인프라가 준비된 뒤 관리자 콘솔에서
  켠다(안전측 기본값). rm 등 파괴적 명령은 아예 등록하지 않는다.
- 새 명령을 추가하려면 여기에 안전한 핸들러를 구현하고 WHITELIST에 등록한 뒤 재배포한다.

주의(인프라 요건): 이 명령들이 의미를 가지려면 system-mcp가 root로, 호스트의 사용자 계정과
파일시스템/네임스페이스에 접근할 수 있어야 한다(베어메탈 또는 호스트 네임스페이스 공유 +
LDAP/SSSD). 일반 컨테이너에는 직원 OS 계정이 없어 pwd 조회가 실패하고 실행이 거부된다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from linux_exec import run_as_user, safe_path, build_find_argv  # noqa: E402


async def list_dir(user_id: str, path: str = ".", show_hidden: bool = False) -> dict:
    """디렉토리 내용을 자세히(ls -l) 나열한다. show_hidden=True면 숨김 파일도 포함(-a)."""
    argv = ["ls", "-lh", "-a" if show_hidden else "-A", "--", safe_path(path)]
    return await run_as_user(user_id, argv)


async def find_files(user_id: str, path: str = ".", name_pattern: str | None = None,
                     type: str | None = None, max_depth: int | None = None) -> dict:
    """파일/디렉토리를 검색한다(find, 읽기 전용). name_pattern은 glob(예: '*.log'),
    type은 f/d/l, max_depth로 깊이 제한. -exec/-delete 등 실행·삭제 옵션은 지원하지 않는다."""
    return await run_as_user(user_id, build_find_argv(path, name_pattern, type, max_depth))


async def disk_free(user_id: str) -> dict:
    """파일시스템별 디스크 사용/여유 용량을 사람이 읽기 쉬운 단위로 보여준다(df -h)."""
    return await run_as_user(user_id, ["df", "-h"])


async def disk_usage(user_id: str, path: str = ".", max_depth: int = 1) -> dict:
    """경로의 디스크 사용량을 보여준다(du). max_depth로 하위 깊이를 제한한다."""
    md = int(max_depth)
    if md < 0 or md > 10:
        raise ValueError("max_depth는 0~10 사이여야 합니다.")
    return await run_as_user(user_id, ["du", "-h", f"--max-depth={md}", "--", safe_path(path)])


async def read_file_head(user_id: str, path: str, lines: int = 200) -> dict:
    """텍스트 파일의 앞부분을 보여준다(head -n). 호출자 권한으로만 읽는다."""
    n = int(lines)
    if n < 1 or n > 2000:
        raise ValueError("lines는 1~2000 사이여야 합니다.")
    return await run_as_user(user_id, ["head", "-n", str(n), "--", safe_path(path)])


async def system_info(user_id: str, kind: str = "uptime") -> dict:
    """시스템 정보를 조회한다. kind: uptime | memory | network | who.
    network는 IP/인터페이스 정보(ip addr)를 보여준다."""
    table = {
        "uptime": ["uptime"],
        "memory": ["free", "-h"],
        "network": ["ip", "addr"],
        "who": ["who"],
    }
    if kind not in table:
        raise ValueError("kind는 uptime|memory|network|who 중 하나입니다.")
    return await run_as_user(user_id, table[kind])


# name -> 실행 핸들러와 메타데이터.
#  - enabled: 최초 기동 시 기본 활성 여부(이후 관리자 콘솔 토글이 우선). 리눅스 실행은 기본 OFF.
#  - required_roles: 지정하면 해당 역할 보유자만 실행(콘솔 편집, 실시간 반영). X-User-Roles로 검증.
#  - user_scoped: True면 user_id를 LLM 스키마에서 감추고 호출자 신원에서 강제 주입.
_COMMON = {"enabled": False, "required_roles": [], "user_scoped": True, "scope_param": "user_id"}

WHITELIST = {
    "list_dir": {"handler": list_dir,
                 "description": ("현재 로그인 사용자 '본인 권한'으로 서버 디렉토리 내용을 나열한다"
                                 "(ls). 파일이 있는지/이름이 무엇인지 확인할 때 사용한다."), **_COMMON},
    "find_files": {"handler": find_files,
                   "description": ("현재 사용자 권한으로 서버에서 파일을 검색한다(find, 읽기 전용). "
                                   "이름 패턴(예: '*.log')이나 종류로 찾을 때 사용한다. "
                                   "파일을 수정/삭제/실행하지는 못한다."), **_COMMON},
    "disk_free": {"handler": disk_free,
                  "description": "서버 파일시스템별 디스크 여유/사용 용량을 조회한다(df -h).", **_COMMON},
    "disk_usage": {"handler": disk_usage,
                   "description": ("특정 경로가 차지하는 디스크 용량을 조회한다(du). "
                                   "'어느 폴더가 용량을 많이 쓰나'를 볼 때 사용한다."), **_COMMON},
    "read_file_head": {"handler": read_file_head,
                       "description": ("현재 사용자 권한으로 텍스트 파일 앞부분을 읽는다(head). "
                                       "로그/설정 파일 내용을 확인할 때 사용한다."), **_COMMON},
    "system_info": {"handler": system_info,
                    "description": ("서버 시스템 정보를 조회한다: uptime(가동시간)/memory(메모리)/"
                                    "network(IP·인터페이스)/who(접속자). kind로 무엇을 볼지 지정한다."),
                    **_COMMON},
}
