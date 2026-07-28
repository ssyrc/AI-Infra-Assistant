"""
커맨드 카탈로그(command_catalog)에 등록된 커맨드를 '실행 가능한 argv'로 만드는 공통 로직.

정책(관리자 결정):
  카탈로그(매뉴얼 엑셀 업로드본)에 올라간 커맨드는 별도 등록 없이 전부 실행 가능하다.
  실행 여부를 항목별로 켜고 끄는 '화이트리스트 관리'는 System MCP에서만 한다.

화이트리스트가 없어도 구조적 안전장치는 그대로 유지된다:
  - 셸을 쓰지 않는다(argv 리스트 그대로 exec) -> 인자에 셸 메타문자가 있어도 인젝션 불가.
  - 항상 호출자 본인 권한으로 강등해 실행된다(ssh_exec.run_ssh_as_user의 `su - <user_id>`).
    root로 실행되는 경로가 없다.
  - 대상 서버는 /etc/hosts에 등록된 서버만 허용된다(ssh_exec.resolve_host).
  - 타임아웃/출력 상한이 걸린다.
  - 파괴적인 '기본 명령'은 실행 시점에 거부한다(아래 deny 목록. 관리자 콘솔 설정
    `catalog_exec_deny_commands`로 조정 가능하고, 비우면 제한 없이 전부 실행된다).

실행 커맨드(exec_command)는 카탈로그 열에서 온다. 비어 있으면 커맨드 이름(name)을 그대로 쓴다.
`{user_id}` 토큰은 호출자 본인 계정으로 치환된다(예: `phd info -u {user_id}`).
"""
import shlex

from custom_commands import DENY_BASE_COMMANDS

# 카탈로그 실행에서 기본으로 막는 파괴적/권한상승 기본 명령(System MCP 커스텀 커맨드와 동일 목록).
DEFAULT_DENY_CSV = ",".join(sorted(DENY_BASE_COMMANDS))

# 셸을 쓰지 않으므로 파이프/리다이렉션은 동작하지 않는다. 조용히 이상하게 실행되는 대신
# 무엇이 문제인지 알려주기 위해 명시적으로 거부한다.
_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "`", "$("}

MAX_ARGS = 32
MAX_ARG_LEN = 512


def deny_set(csv_value: str | None) -> set[str]:
    """설정값(콤마 구분 문자열)을 deny 집합으로 바꾼다. 빈 값이면 제한 없음."""
    if csv_value is None:
        csv_value = DEFAULT_DENY_CSV
    return {t.strip().lower() for t in csv_value.split(",") if t.strip()}


def _check_token(token: str, where: str) -> str:
    if not isinstance(token, str):
        raise ValueError(f"{where}는 문자열이어야 합니다: {token!r}")
    if "\x00" in token or any(ord(c) < 32 for c in token):
        raise ValueError(f"{where}에 제어문자가 들어갈 수 없습니다.")
    if len(token) > MAX_ARG_LEN:
        raise ValueError(f"{where}가 너무 깁니다(최대 {MAX_ARG_LEN}자).")
    if token.strip() in _SHELL_OPERATORS:
        raise ValueError(
            f"셸 연산자({token.strip()})는 지원하지 않습니다. "
            "파이프/리다이렉션 없이 실행 가능한 단일 커맨드로 등록해 주세요.")
    return token


def build_catalog_argv(exec_command: str | None, name: str, args: list | None,
                       user_id: str, deny: set[str]) -> list[str]:
    """카탈로그 행 + 호출자가 준 인자로 실행 argv를 만든다.

    exec_command: 카탈로그의 '실행 커맨드' 열(비면 name을 그대로 실행).
    args: LLM/사용자가 추가로 붙이는 인자 목록(각 원소가 argv 한 칸, 셸 파싱 없음).
    user_id: `{user_id}` 토큰 치환값(호출자 본인 계정).
    deny: 거부할 기본 명령 집합(deny_set()).
    """
    raw = (exec_command or "").strip() or (name or "").strip()
    if not raw:
        raise ValueError("실행할 커맨드가 비어 있습니다.")
    try:
        base_argv = shlex.split(raw)
    except ValueError as e:
        raise ValueError(f"실행 커맨드를 해석할 수 없습니다({raw!r}): {e}")
    if not base_argv:
        raise ValueError("실행할 커맨드가 비어 있습니다.")

    argv = [_check_token(t, "실행 커맨드") for t in base_argv]
    argv = [user_id if t == "{user_id}" else t.replace("{user_id}", user_id) for t in argv]

    base = argv[0].strip().lower().rsplit("/", 1)[-1]   # /bin/rm 같은 경로 우회 차단
    if base in deny:
        raise PermissionError(
            f"'{base}'는 파괴적이거나 권한 상승 위험이 있어 실행할 수 없습니다"
            "(관리자 콘솔 설정 catalog_exec_deny_commands).")

    # LLM이 args를 리스트가 아니라 문자열 한 줄로 주는 경우가 있다("-l /home").
    # 그대로 list()로 감싸면 글자 단위로 쪼개지므로 셸 규칙대로 토큰화해 준다.
    if isinstance(args, str):
        try:
            extra = shlex.split(args)
        except ValueError as e:
            raise ValueError(f"인자를 해석할 수 없습니다({args!r}): {e}")
    else:
        extra = list(args or [])
    if len(extra) > MAX_ARGS:
        raise ValueError(f"인자가 너무 많습니다(최대 {MAX_ARGS}개).")
    argv += [_check_token(str(a), "인자") for a in extra]
    return argv
