"""
호출자(user_id) 권한으로 안전하게 read-only 리눅스 명령을 실행하는 유틸.

안전 설계 (매우 중요 — 이 프로세스는 root로 돌 수 있다):
- 셸을 절대 쓰지 않는다(shell=False). 명령은 argv 리스트로만 실행하므로 `;`, `|`, `$()`,
  백틱 같은 셸 메타문자로 인한 명령 주입이 원천적으로 불가능하다.
- 실행 직전 preexec_fn에서 대상 사용자(uid/gid)로 강등한다(setgid/initgroups/setuid).
  user_id는 실제 OS 계정명이어야 하며(pwd.getpwnam으로 검증), 없으면 실행하지 않는다.
- root가 아니어서 setuid가 불가능하면 예외가 나고 실행은 중단된다(권한 상승 없음).
- 타임아웃과 출력 크기 상한을 강제한다(예: find / 로 인한 폭주 방지).
- 파괴적 동작 경로는 애초에 노출하지 않는다. 특히 find는 -exec/-delete 등 실행·삭제
  프레디킷을 전면 금지한다.
"""
import os
import pwd
import asyncio

MAX_OUTPUT = 64 * 1024      # 64KB
DEFAULT_TIMEOUT = 15        # 초

# find에서 절대 허용하지 않는 프레디킷(임의 실행/삭제/쓰기 경로).
_FIND_FORBIDDEN = {
    "-exec", "-execdir", "-ok", "-okdir",
    "-delete", "-fprint", "-fprint0", "-fprintf", "-fls",
}


def resolve_user(user_id: str) -> pwd.struct_passwd:
    """user_id가 실제 OS 계정인지 검증하고 pwd 엔트리를 돌려준다."""
    if not user_id or not isinstance(user_id, str):
        raise PermissionError("사용자 식별자가 없습니다.")
    try:
        return pwd.getpwnam(user_id)
    except KeyError:
        raise PermissionError(
            f"OS 사용자 '{user_id}'가 이 호스트에 없어 명령을 실행할 수 없습니다.")


def safe_path(p: str | None, default: str = ".") -> str:
    """경로 인자를 검증한다. 널바이트/제어문자를 막는다(셸이 없으므로 메타문자는 무해)."""
    if p is None:
        return default
    if not isinstance(p, str) or "\x00" in p or any(ord(c) < 32 for c in p):
        raise ValueError("잘못된 경로입니다.")
    return p


async def run_as_user(user_id: str, argv: list[str],
                      timeout: int = DEFAULT_TIMEOUT, max_output: int = MAX_OUTPUT) -> dict:
    """user_id 권한으로 argv를 실행하고 stdout/stderr/exit_code를 돌려준다(셸 없음)."""
    pw = resolve_user(user_id)
    uid, gid = pw.pw_uid, pw.pw_gid
    home = pw.pw_dir if os.path.isdir(pw.pw_dir) else "/"

    def _demote():
        # 그룹 -> 보조그룹 -> 사용자 순으로 강등(순서 중요). root가 아니면 여기서 실패한다.
        os.setgid(gid)
        os.initgroups(user_id, gid)
        os.setuid(uid)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_demote,
            cwd=home,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "HOME": home, "USER": user_id, "LANG": "C"},
        )
    except PermissionError:
        raise PermissionError("권한 강등(setuid)에 실패했습니다. system-mcp가 root로 실행 중인지 확인하세요.")

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise TimeoutError(f"명령이 {timeout}초 안에 끝나지 않아 중단했습니다.")

    def _clip(b: bytes) -> str:
        s = b.decode("utf-8", "replace")
        return s if len(s) <= max_output else s[:max_output] + "\n…(출력 잘림)"

    return {"exit_code": proc.returncode, "stdout": _clip(out), "stderr": _clip(err)}


def build_find_argv(path: str, name_pattern: str | None, ftype: str | None,
                    max_depth: int | None) -> list[str]:
    """안전한 find argv를 만든다. 위험 프레디킷은 애초에 넣지 않고, 방어적으로 재검증한다."""
    argv = ["find", safe_path(path)]
    if max_depth is not None:
        md = int(max_depth)
        if md < 0 or md > 20:
            raise ValueError("max_depth는 0~20 사이여야 합니다.")
        argv += ["-maxdepth", str(md)]
    if ftype:
        if ftype not in ("f", "d", "l"):
            raise ValueError("type은 f(파일)/d(디렉토리)/l(링크) 중 하나입니다.")
        argv += ["-type", ftype]
    if name_pattern:
        if "\x00" in name_pattern:
            raise ValueError("잘못된 name_pattern입니다.")
        argv += ["-name", name_pattern]
    # 방어적 재검증: 어떤 경로로든 금지 프레디킷이 들어오면 거부한다.
    if any(a in _FIND_FORBIDDEN for a in argv):
        raise ValueError("허용되지 않는 find 옵션입니다.")
    return argv
