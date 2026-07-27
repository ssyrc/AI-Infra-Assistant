"""
관리자 콘솔에서 코드 배포 없이 등록하는 System MCP 화이트리스트 커맨드 공통 로직.

argv_template은 문자열 리스트다("{param}" 토큰만 params에 정의된 값으로 치환되고,
그 외 토큰은 그대로 argv 원소가 된다). 셸을 쓰지 않고 argv 그대로 실행되므로(ssh_exec 참고)
값 안에 셸 메타문자가 있어도 인젝션이 안 되지만, 파괴적인 기본 명령 자체는 이름으로 막는다.
"""
import re

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PARAM_TYPES = {"str": str, "int": int}

# 완전한 안전장치가 아니라, 명백히 파괴적인 기본 명령을 막는 최소한의 안전망이다.
DENY_BASE_COMMANDS = {
    "rm", "dd", "mkfs", "shutdown", "reboot", "poweroff", "halt", "init",
    "kill", "killall", "pkill", "useradd", "userdel", "usermod", "groupadd",
    "groupdel", "passwd", "visudo", "iptables", "systemctl", "service",
    "mount", "umount", "chmod", "chown", "chattr", "fdisk", "parted",
    "mkswap", "shred", "wipefs", "crontab", "su", "sudo",
}


def validate_definition(tool_name: str, argv_template: list, params: list, existing_names: set) -> None:
    """등록/수정 시 검증. 문제가 있으면 ValueError를 던진다."""
    if not TOOL_NAME_RE.match(tool_name or ""):
        raise ValueError("이름은 소문자/숫자/밑줄만, 소문자로 시작해야 합니다(예: disk_iostat).")
    if tool_name in existing_names:
        raise ValueError(f"이미 있는 이름입니다: {tool_name}")

    if not isinstance(argv_template, list) or not argv_template or not all(
        isinstance(t, str) and t for t in argv_template
    ):
        raise ValueError("커맨드는 비어있지 않은 문자열 토큰의 리스트여야 합니다.")
    base = argv_template[0].strip().lower()
    if base in DENY_BASE_COMMANDS:
        raise ValueError(f"'{base}'는 파괴적이거나 권한 상승 위험이 있어 등록할 수 없습니다.")

    seen = set()
    for p in params or []:
        name = p.get("name", "")
        if not PARAM_NAME_RE.match(name):
            raise ValueError(f"파라미터 이름이 올바르지 않습니다: {name!r}")
        if name in ("user_id", "host"):
            raise ValueError(f"'{name}'는 예약된 이름입니다(자동 주입됨).")
        if name in seen:
            raise ValueError(f"파라미터 이름이 중복됩니다: {name}")
        seen.add(name)
        if p.get("type", "str") not in PARAM_TYPES:
            raise ValueError(f"지원하지 않는 파라미터 타입입니다: {p.get('type')}")

    placeholders = {t[1:-1] for t in argv_template if t.startswith("{") and t.endswith("}")}
    unknown = placeholders - seen
    if unknown:
        raise ValueError(f"커맨드에 있는 파라미터가 정의되지 않았습니다: {', '.join(sorted(unknown))}")


def render_argv(argv_template: list, values: dict) -> list:
    out = []
    for token in argv_template:
        if token.startswith("{") and token.endswith("}") and token[1:-1] in values:
            out.append(str(values[token[1:-1]]))
        else:
            out.append(token)
    return out
