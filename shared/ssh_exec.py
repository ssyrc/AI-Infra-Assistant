"""
호출자(user_id) 권한으로 '원격 서버'에서 read-only 명령을 실행하는 유틸(System/Command MCP 공용).

토폴로지:
- 이 agent 호스트(예: 202.20.183.30)는 root로 뜬다. 대상 서버들에 root로 ssh 할 수 있다.
- 대상은 **IP로 직접 지정**하는 것을 원칙으로 한다(설정 `scheduler_login_host`).
  이름을 쓰면 이 호스트의 /etc/hosts에서 찾는다. 미등록 이름은 거부한다.
  이름 해석은 우리가 통제하지 못하는 파일에 의존해, 같은 이름이 다른 서버로 풀리면
  키가 등록되지 않은 곳에 붙어 전부 인증 실패한다(실제로 login07이 그랬다).
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
import ipaddress

HOSTS_FILE = os.environ.get("HOSTS_FILE", "/etc/hosts")
SSH_ROOT_USER = os.environ.get("SSH_ROOT_USER", "root")
SSH_KEY = os.environ.get("SSH_KEY", "")
# 컨테이너는 자주 재생성돼 known_hosts가 남지 않는다. 컨테이너 안 경로를 명시해 두면
# 매번 새로 만들어져도 문제가 없고, 호스트의 known_hosts와 충돌하지도 않는다.
SSH_KNOWN_HOSTS = os.environ.get("SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts_agent")
# accept-new: 처음 보는 호스트는 자동 등록, 등록된 키와 '다르면' 거부(기본).
# 게이트 서버 키가 바뀌어 계속 막히면 .env에 SSH_STRICT_HOST_KEY=no 로 완화할 수 있다.
SSH_STRICT_HOST_KEY = os.environ.get("SSH_STRICT_HOST_KEY", "accept-new")
# `su - <user> -c ...`는 원격에 TTY가 없으면 PAM 설정에 따라 인증 단계에서 실패할 수 있다
# (`docker compose exec`로 손으로 돌리면 TTY가 붙어서 되는데 에이전트에서만 안 되는 원인).
# ssh -tt로 TTY를 강제해 손으로 돌릴 때와 같은 조건을 만든다. 문제가 있으면 .env에서 끈다.
# 기본 false: 컨테이너에서 TTY 없이(`exec -T`) 실행해도 `su - <user> -c ...`가 정상 동작함을
# 확인했다. -tt는 출력에 CR/제어문자를 섞으므로, 필요한 환경에서만 .env로 켠다.
SSH_FORCE_TTY = os.environ.get("SSH_FORCE_TTY", "false").strip().lower() == "true"
try:
    SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "8"))
except ValueError:
    SSH_CONNECT_TIMEOUT = 8

MAX_OUTPUT = 64 * 1024
# 사내 커맨드는 느린 게 많다(GPFS 쿼터 조회처럼 스토리지 전체를 훑는 것들). 25초는 너무 짧아서
# 정상 동작하는 커맨드가 중단되고, 그러면 에이전트가 실패 원인을 엉뚱하게 해석한다.
# .env의 SSH_COMMAND_TIMEOUT으로 조정한다.
try:
    DEFAULT_TIMEOUT = int(os.environ.get("SSH_COMMAND_TIMEOUT", "120"))
except ValueError:
    DEFAULT_TIMEOUT = 120

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
    """접속할 대상의 IP를 돌려준다.

    **IP를 직접 주면 그 IP로 그대로 붙는다.** 이름은 HOSTS_FILE에서 찾는다.

    왜 IP를 우선하나: 배포 호스트의 /etc/hosts에서 `login07`이 게이트 서버가 아니라
    전혀 다른 서버(75.11.29.7)로 풀리고 있었고, 그 서버에는 우리 키가 등록돼 있지 않아
    모든 커맨드 실행이 인증 실패했다. 이름 해석은 우리가 통제할 수 없는 파일에 의존하므로,
    로그인 서버는 설정에 **IP로** 박아 두고 이름 해석 자체를 타지 않게 한다.
    """
    target = (name or "").strip()
    try:
        # IPv4/IPv6 리터럴이면 이름 해석을 건너뛴다(/etc/hosts에 없어도 된다).
        ipaddress.ip_address(target)
        return target
    except ValueError:
        pass
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
    raise ValueError(
        f"{HOSTS_FILE}에 등록되지 않은 서버 이름입니다: {target}. "
        "이름 대신 IP로 지정하면 이름 해석을 타지 않습니다(예: 202.20.185.100).")


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
        "-o", f"StrictHostKeyChecking={SSH_STRICT_HOST_KEY}",
        "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    ]
    if SSH_FORCE_TTY:
        ssh_argv.append("-tt")
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
            stdin=asyncio.subprocess.DEVNULL,   # -tt로 pty를 붙여도 입력 대기에 걸리지 않게
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
        raise TimeoutError(
            f"명령이 {timeout}초 안에 끝나지 않아 중단했습니다({host}). 원래 오래 걸리는 "
            "커맨드라면 .env의 SSH_COMMAND_TIMEOUT을 늘리세요(권한/인증 문제가 아닙니다).")

    def _clip(b: bytes) -> str:
        # -tt로 pty를 쓰면 줄바꿈이 CRLF로 오고 "Connection to ... closed." 안내가 붙는다.
        s = b.decode("utf-8", "replace").replace("\r\n", "\n")
        s = "\n".join(line for line in s.split("\n")
                       if not line.startswith("Connection to ") or not line.endswith("closed."))
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
    # **어느 IP로 붙었는지를 반드시 함께 적는다** - 손으로 IP를 직접 넣으면 되는데 에이전트만
    # 실패하는 경우, 원인은 거의 항상 "이름이 /etc/hosts에서 다른 IP로 풀렸다"이기 때문이다.
    low = result["stderr"].lower()
    # ssh가 남긴 진짜 사유 한 줄(디버그 잡음은 빼고). 추측 대신 이걸 보여준다.
    reason = next((ln.strip() for ln in result["stderr"].split("\n")
                   if ln.strip() and not ln.startswith("debug")), "")
    where = f"'{host}'({ip})"
    if proc.returncode == 255 and "host key verification" in low:
        result["error"] = (
            f"{where}의 ssh 호스트 키 확인에 실패해 커맨드가 실행되지 않았습니다"
            f"(인증서/계정 문제가 아님). known_hosts({SSH_KNOWN_HOSTS})에 다른 키가 등록돼 "
            f"있거나 StrictHostKeyChecking={SSH_STRICT_HOST_KEY} 설정이 막고 있습니다. "
            "컨테이너에서 해당 호스트 키를 지우거나(.env에 SSH_STRICT_HOST_KEY=no) 재시도하세요.")
    elif proc.returncode == 255 and ("permission denied" in low or "publickey" in low):
        if not SSH_KEY:
            detail = "SSH_KEY가 설정되지 않았습니다."
        elif not os.path.isfile(SSH_KEY):
            detail = (f"SSH_KEY 경로가 파일이 아닙니다({SSH_KEY}). compose가 없는 경로를 마운트해 "
                      "빈 디렉토리가 생긴 상태일 수 있습니다 - .env의 SSH_KEY_PATH를 실제 개인키 "
                      "파일로 지정하세요.")
        else:
            # 키 파일은 멀쩡한데 거부당했다면, 키가 아니라 '접속한 서버'가 다를 가능성이 크다.
            detail = (f"키 파일({SSH_KEY})은 정상입니다. '{host}'가 {HOSTS_FILE}에서 {ip}로 "
                      f"풀렸는데, 이 키가 등록된 서버가 {ip}가 맞는지 확인하세요"
                      "(이름이 의도한 서버가 아닌 다른 IP로 풀리는 경우가 가장 흔합니다).")
        result["error"] = (f"{where}에 ssh 인증이 실패해 커맨드가 실행되지 않았습니다"
                           f"(사용자 권한 문제가 아님). {detail}"
                           + (f" ssh 메시지: {reason}" if reason else ""))
    elif proc.returncode == 255:
        # 위 두 갈래에 안 걸리는 접속 실패(네트워크 불가, 타임아웃 등)도 그냥 넘기지 않는다.
        result["error"] = (f"{where}에 ssh 접속 자체가 실패했습니다."
                           + (f" ssh 메시지: {reason}" if reason else ""))
    elif proc.returncode != 0 and any(m in low for m in _NO_SUCH_USER):
        result["error"] = (
            f"서버 '{host}'에 '{user}' 계정이 없어 실행하지 못했습니다(권한 문제가 아님). "
            "Open WebUI 계정 이메일의 '@' 앞부분이 서버 계정명과 같아야 합니다.")
    return result
