"""
System MCP 명령용 인자 검증/빌더(순수 함수). 실제 실행은 shared/ssh_exec.run_ssh_as_user가
원격 서버에서 사용자 권한으로 수행한다. 여기서는 안전한 argv를 만드는 일만 한다.
"""
# find에서 절대 허용하지 않는 프레디킷(임의 실행/삭제/쓰기 경로).
_FIND_FORBIDDEN = {
    "-exec", "-execdir", "-ok", "-okdir",
    "-delete", "-fprint", "-fprint0", "-fprintf", "-fls",
}


def safe_path(p: str | None, default: str = ".") -> str:
    """경로 인자를 검증한다. 널바이트/제어문자를 막는다(셸 인용은 ssh_exec가 담당)."""
    if p is None:
        return default
    if not isinstance(p, str) or "\x00" in p or any(ord(c) < 32 for c in p):
        raise ValueError("잘못된 경로입니다.")
    return p


def build_find_argv(path: str, name_pattern: str | None, ftype: str | None,
                    max_depth: int | None) -> list:
    """안전한 find argv를 만든다. 위험 프레디킷은 넣지 않고 방어적으로 재검증한다."""
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
    if any(a in _FIND_FORBIDDEN for a in argv):
        raise ValueError("허용되지 않는 find 옵션입니다.")
    return argv
