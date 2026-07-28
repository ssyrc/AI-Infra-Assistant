"""
호출자(user_id) 권한으로 '원격 서버'에서 read-only 명령을 실행하는 유틸(System/Command MCP 공용).

토폴로지:
- 이 agent 호스트(예: 202.20.183.30)는 root로 뜬다. 대상 서버들에 root로 ssh 할 수 있다.
- 대상 서버의 IP는 이 호스트의 /etc/hosts에 등록돼 있다(예: `202.20.185.100  login05`).
  => 등록된 호스트만 접근 가능(화이트리스트). 미등록 이름은 거부한다.
- 실행: ssh root@<ip> 로 접속한 뒤, 원격에서 `su - <user_id> -c ...`로 '사용자 권한'으로 강등해
  명령을 실행한다. 남의 권한으로 실행할 수 없다.

보안:
- 로컬에서 셸을 쓰지 않는다(create_subprocess_exec, argv 리스트). 원격 명령 문자열은 shlex로
  이중 quote한다(root 셸 1회 + 사용자 셸 1회). host는 /etc/hosts 조회로만 얻고, user_id는
  엄격한 리눅스 계정명 정규식으로 검증하므로 메타문자가 들어갈 수 없다.
- BatchMode/PasswordAuthentication=no로 비밀번호 프롬프트에 걸려 멈추지 않는다.
- 타임아웃/출력 상한을 강제한다. rm 등 파괴적 명령은 상위(화이트리스트)에서 아예 노출하지 않는다.

환경변수(컨테이너에서 주입):
- HOSTS_FILE           대상 IP 매핑 파일 경로(기본 /etc/hosts)
- SSH_ROOT_USER        ssh 접속 계정(기본 root)
- SSH_KEY              ssh 개인키 경로(있으면 -i 로 사용)
- SSH_CONNECT_TIMEOUT  접속 타임아웃 초(기본 8)
"""
import os
import re
import shlex
import asyncio

HOSTS_FILE = os.environ.get("HOSTS_FILE", "/etc/hosts")
SSH_ROOT_USER = os.environ.get("SSH_ROOT_USER", "root")
SSH_KEY = os.environ.get("SSH_KEY", "")
try:
    SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "8"))
except ValueError:
    SSH_CONNECT_TIMEOUT = 8

MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 25

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")
# 리눅스 계정명 형식. 사내 계정이 `yr9.choi`처럼 점을 포함하므로 '.'을 허용한다.
# 첫 글자를 [a-z_]로 고정해서 '-'로 시작하는 이름(ssh/su 옵션으로 해석될 수 있음)을 막는다.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$")

# 이 계정들로는 절대 실행하지 않는다(uid 0). agent 컨테이너가 root로 ssh하므로,
# user_id가 root로 들어오면 강등 없이 root로 실행되어 "절대 root 금지" 원칙이 깨진다.
DENY_USERS = {"root", "toor"}

# `su`가 계정을 못 찾았을 때의 대표적인 stderr 문구(배포판마다 조금씩 다름).
_NO_SUCH_USER = ("does not exist", "no passwd entry", "unknown id", "user not found")


def resolve_host(name: str) -> str:
    """호스트명(또는 IP)을 HOSTS_FILE에서 찾아 IP를 돌려준다.
    등록되지 않은 호스트는 거부한다(= /etc/hosts가 접근 대상 화이트리스트)."""
    target = (name or "").strip()
    if not _HOSTNAME_RE.match(target):
        raise ValueError(f"잘못된 호스트명입니다: {name!r}")
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as e:
        raise RuntimeError(f"{HOSTS_FILE}를 읽을 수 없습니다: {e}")
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ip, names = parts[0], parts[1:]
        if target == ip or target in names:
            return ip
    raise ValueError(f"/etc/hosts에 등록되지 않은 서버입니다: {target} (등록된 서버만 접근 가능)")


def validate_user(user_id: str) -> str:
    if not user_id or not _USER_RE.match(user_id):
        raise PermissionError(
            f"리눅스 계정명 형식이 아니어서 실행할 수 없습니다: {user_id!r}. "
            "Open WebUI 계정 이메일의 '@' 앞부분이 서버 계정명과 같아야 합니다.")
    if user_id in DENY_USERS:
        raise PermissionError(
            f"'{user_id}' 계정으로는 실행할 수 없습니다. 커맨드는 반드시 일반 사용자 권한으로만 "
            "실행됩니다. 서버 계정과 연결된 일반 사용자 계정으로 로그인해 주세요.")
    return user_id


def _remote_command(user: str, argv: list) -> str:
    """원격에서 'su - user -c <inner>' 형태로 사용자 권한 실행 명령을 만든다.
    inner의 동적 인자는 사용자 셸용으로 quote하고, inner 전체는 root 셸용으로 다시 quote한다."""
    inner = " ".join(shlex.quote(str(a)) for a in argv)     # 사용자 셸 파싱용
    return f"su - {user} -c {shlex.quote(inner)}"            # root 셸 파싱용 (user는 정규식 검증됨)


async def run_ssh_as_user(host: str, user_id: str, argv: list,
                          timeout: int = DEFAULT_TIMEOUT, max_output: int = MAX_OUTPUT) -> dict:
    """host(=/etc/hosts 등록)로 ssh(root) 후 user_id 권한으로 argv를 실행한다(셸 주입 불가)."""
    ip = resolve_host(host)
    user = validate_user(user_id)
    remote_cmd = _remote_command(user, argv)

    ssh_argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    ]
    # SSH_KEY 경로가 '파일'일 때만 -i로 넘긴다. compose가 없는 경로를 bind mount하면 도커가
    # 그 자리에 '빈 디렉토리'를 만들어 버리는데, 그걸 -i로 주면 ssh가 무조건 인증 실패한다
    # (서버에서 직접 ssh하면 되는데 에이전트로만 안 되는 전형적인 원인).
    if SSH_KEY and os.path.isfile(SSH_KEY):
        ssh_argv += ["-i", SSH_KEY]
    elif SSH_KEY:
        print(f"[ssh_exec] SSH_KEY가 파일이 아니라 무시합니다: {SSH_KEY} "
              "(docker가 빈 디렉토리를 만든 상태일 수 있음 - .env의 SSH_KEY_PATH 확인)")
    ssh_argv += [f"{SSH_ROOT_USER}@{ip}", remote_cmd]

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("ssh 클라이언트가 없습니다. MCP 컨테이너에 openssh-client가 필요합니다.")

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise TimeoutError(f"명령이 {timeout}초 안에 끝나지 않아 중단했습니다({host}).")

    def _clip(b: bytes) -> str:
        s = b.decode("utf-8", "replace")
        return s if len(s) <= max_output else s[:max_output] + "\n…(출력 잘림)"

    result = {
        "host": host,
        "ip": ip,
        "as_user": user,
        "command": " ".join(str(a) for a in argv),
        "exit_code": proc.returncode,
        "stdout": _clip(out),
        "stderr": _clip(err),
    }
    # 실패 원인을 에이전트가 엉뚱하게 해석하지 않도록, 흔한 두 가지는 명시적으로 알려준다.
    low = result["stderr"].lower()
    if proc.returncode == 255 and ("permission denied" in low or "publickey" in low
                                   or "host key verification" in low):
        detail = ""
        if not SSH_KEY:
            detail = "SSH_KEY가 설정되지 않았습니다."
        elif not os.path.isfile(SSH_KEY):
            detail = (f"SSH_KEY 경로가 파일이 아닙니다({SSH_KEY}). compose가 없는 경로를 마운트해 "
                      "빈 디렉토리가 생긴 상태일 수 있습니다 - .env의 SSH_KEY_PATH를 실제 개인키 "
                      "파일로 지정하세요.")
        else:
            detail = f"마운트된 키({SSH_KEY})가 대상 서버에 등록돼 있지 않을 수 있습니다."
        result["error"] = (f"'{host}'에 ssh 인증이 실패해 커맨드가 실행되지 않았습니다"
                           f"(사용자 권한 문제가 아님). {detail}")
    elif proc.returncode != 0 and any(m in low for m in _NO_SUCH_USER):
        result["error"] = (
            f"서버 '{host}'에 '{user}' 계정이 없어 실행하지 못했습니다(권한 문제가 아님). "
            "Open WebUI 계정 이메일의 '@' 앞부분이 서버 계정명과 같아야 합니다.")
    return result
