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
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,63}$")   # 리눅스 계정명 형식


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
        raise PermissionError(f"잘못된 사용자 계정 형식입니다: {user_id!r}")
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
    if SSH_KEY:
        ssh_argv += ["-i", SSH_KEY]
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

    return {
        "host": host,
        "ip": ip,
        "as_user": user,
        "command": " ".join(str(a) for a in argv),
        "exit_code": proc.returncode,
        "stdout": _clip(out),
        "stderr": _clip(err),
    }
