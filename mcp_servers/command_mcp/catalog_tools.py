"""
command_catalog(관리자 콘솔 커맨드 탭)의 행을 **MCP 툴 하나씩**으로 노출한다.

왜 검색(RAG)이 아니라 툴인가:
  예전에는 `search_commands`로 카탈로그를 하이브리드 검색해서 LLM에게 후보를 넘겼다.
  그런데 "내 홈 스토리지 용량"이 `myquota`를 못 찾는 일이 반복됐다 - 검색이 한 번 어긋나면
  등록해 둔 커맨드가 통째로 없는 것처럼 취급된다. 툴로 노출하면 LLM이 **목록을 직접 보고**
  고르므로 그런 실패가 없다. System MCP가 이미 같은 방식이라 두 MCP의 동작도 하나로 맞춰진다.

트레이드오프(알고 택한 것):
  - 툴 목록이 카탈로그 크기만큼 커진다. 설명이 전부 프롬프트에 실리므로 상한을 둔다
    (`command_tools_max`). 넘치면 남는 것은 툴로 못 내보내고 경고를 남긴다.
  - 카탈로그를 고치면 **Command MCP 재시작**이 필요하다(검색은 매번 DB를 읽어 즉시 반영됐다).
    콘솔의 "지금 재시작" 버튼으로 처리한다.

실행 경로는 기존과 완전히 동일하다 - catalog_exec.build_catalog_argv로 argv를 만들고
ssh_exec.run_ssh_as_user로 호출자 본인 권한에서 실행한다(셸 미사용, 파괴적 명령 거부).
"""
import asyncio
import hashlib
import inspect
import os
import re
import sys

import asyncpg

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from ssh_exec import run_ssh_as_user  # noqa: E402
from catalog_exec import DEFAULT_DENY_CSV, build_catalog_argv, deny_set  # noqa: E402

# 실측(2026-07): 툴 하나가 매 요청 프롬프트에서 약 270자 ≈ 100토큰을 쓴다(스키마 고정분 215자
# + 설명). 지시문 ~4.9k토큰, 내장 툴 ~2.7k토큰이 이미 고정으로 나가고 검색 결과·대화 이력에
# 15k 안팎이 필요하므로, 커맨드 툴 예산은 8k토큰(=80개) 정도가 상한이다.
# 그 이상 등록해야 하면 커맨드 설명을 한 줄로 줄이거나 컨텍스트 상한 설정을 함께 낮춰야 한다.
DEFAULT_MAX_TOOLS = 80

# MCP 툴 이름에 쓸 수 있는 문자만 남긴다. 카탈로그 이름에는 공백·점·한글이 들어올 수 있다.
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def _stable_hash(text: str, length: int) -> str:
    """프로세스가 바뀌어도 같은 값이 나오는 짧은 해시(툴 이름 고정용)."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:length]


def tool_name_for(catalog_name: str, taken: set[str], exec_command: str = "") -> str:
    """카탈로그 이름 -> 충돌 없는 툴 이름. 내장 툴과 섞이지 않게 `cmd_` 접두어를 붙인다.

    툴 이름은 ASCII여야 한다(OpenAI 호환 함수 이름 규칙 `[a-zA-Z0-9_-]{1,64}`).
    카탈로그 이름이 한글이면 남는 글자가 없어 전부 같은 이름으로 뭉개지므로,
    **실행 커맨드에서 이름을 만들고**(예: `phd info -u {user_id}` -> `cmd_phd_info`),
    그것도 없으면 원본 이름의 해시를 붙여 서로 구분되게 한다.
    의미는 툴 '설명'이 담고 있으므로 이름은 구분만 되면 된다.
    """
    def _ascii(text: str) -> str:
        # {user_id} 같은 자리표시자와 옵션(-u)은 이름에서 빼고 앞 두 토큰만 쓴다.
        cleaned = re.sub(r"\{[^}]*\}", " ", text or "")
        toks = [t for t in _UNSAFE.sub(" ", cleaned).split() if t and not t.isdigit()]
        return "_".join(toks[:2]).lower()

    base = _ascii(catalog_name) or _ascii(exec_command)
    if not base:
        # 파이썬 hash()는 프로세스마다 값이 달라져(PYTHONHASHSEED) 재시작할 때마다
        # 툴 이름이 바뀐다. 고정 해시를 써야 대화 이력·감사로그와 이름이 어긋나지 않는다.
        base = "k" + _stable_hash(catalog_name, 6)
    if not base[0].isalpha():
        base = f"c_{base}"
    name = f"cmd_{base}"[:60]
    if name not in taken:
        return name
    for i in range(2, 100):
        alt = f"{name[:57]}_{i}"
        if alt not in taken:
            return alt
    return f"{name[:52]}_{_stable_hash(catalog_name, 4)}"


def build_entry(row: dict, login_host_getter) -> dict:
    """카탈로그 한 행을 화이트리스트 항목(build_wrapped가 받는 형태)으로 만든다."""
    catalog_name = row["name"]
    exec_command = row.get("exec_command") or catalog_name

    async def handler(user_id: str, args: list[str] | None = None) -> dict:
        deny = deny_set(DEFAULT_DENY_CSV)
        argv = build_catalog_argv(exec_command, catalog_name, args, user_id, deny)
        return await run_ssh_as_user(await login_host_getter(), user_id, argv)

    # LLM에 보일 파라미터는 args 하나뿐이다(user_id는 호출자 신원에서 강제 주입).
    handler.__signature__ = inspect.Signature([
        inspect.Parameter("user_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        inspect.Parameter("args", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=None, annotation=list),
    ])
    handler.__annotations__ = {"user_id": str, "args": list}

    # 설명은 **짧게**. 툴 하나당 (이름 + 설명 + 스키마)가 전부 매 요청 프롬프트에 실린다.
    # 공통 설명("args에 인자를 나눠 넣는다", "본인 권한으로 실행된다")은 툴마다 반복하면
    # 커맨드 수만큼 곱해지므로 지시문에 한 번만 두고 여기서는 뺀다.
    desc = (row.get("description") or "").strip()
    usage = (row.get("usage") or "").strip()
    parts = [desc] if desc else []
    parts.append(f"[{exec_command}]")
    if usage and usage != exec_command:
        parts.append(usage)

    return {
        "handler": handler,
        "description": " ".join(parts)[:300],
        "enabled": True,
        "required_roles": [],
        "user_scoped": True,
        "scope_param": "user_id",
    }


# 툴 하나가 프롬프트에서 차지하는 JSON 스키마의 고정분(이름·파라미터 틀). 설명 길이와 무관하게
# 항상 따라붙는다 - 측정값 약 215자. 여기에 설명 길이를 더한 것이 이 툴의 실제 프롬프트 비용이다.
_TOOL_SCHEMA_OVERHEAD = 215


def estimate_prompt_tokens(descriptions: list[str]) -> tuple[int, int]:
    """노출된 툴들이 매 요청 프롬프트에서 쓰는 (글자 수, 대략의 토큰 수).

    토큰 수는 추정이다 - 폐쇄망에 Qwen 토크나이저를 두고 세는 대신, 한글은 1.2자/토큰,
    나머지(ASCII·JSON 기호)는 3.5자/토큰으로 환산한다. 자릿수를 보려는 값이지 정밀값이 아니다.
    """
    chars = tokens = 0
    for d in descriptions:
        text = (d or "") + " " * _TOOL_SCHEMA_OVERHEAD
        kor = len(re.findall(r"[가-힣]", text))
        chars += len(text)
        tokens += round(kor / 1.2 + (len(text) - kor) / 3.5)
    return chars, tokens


def load_catalog_tools_sync(login_host_getter, dsn_key: str = "command_db_dsn") -> tuple[dict, int]:
    """(툴 dict, 상한 때문에 빠진 개수)를 돌려준다. 기동 시 1회 호출."""
    async def _run():
        config_dsn = os.environ.get("CONFIG_DB_DSN")
        if not config_dsn:
            return {}, 0
        conn = await asyncpg.connect(config_dsn)
        try:
            dsn = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = $1", dsn_key)
            raw_max = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = 'command_tools_max'")
        finally:
            await conn.close()
        if not dsn:
            return {}, 0
        try:
            max_tools = int(raw_max) if raw_max else DEFAULT_MAX_TOOLS
        except (TypeError, ValueError):
            max_tools = DEFAULT_MAX_TOOLS

        c2 = await asyncpg.connect(dsn)
        try:
            total = await c2.fetchval("SELECT count(*) FROM command_catalog")
            rows = await c2.fetch(
                "SELECT name, description, usage, exec_command FROM command_catalog "
                "ORDER BY name LIMIT $1", max_tools)
        finally:
            await c2.close()

        tools, taken = {}, set()
        for r in rows:
            row = dict(r)
            name = tool_name_for(row["name"], taken, row.get("exec_command") or "")
            taken.add(name)
            tools[name] = build_entry(row, login_host_getter)
        dropped = max(0, (total or 0) - len(rows))
        return tools, dropped

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[command-mcp] 커맨드 카탈로그 로드 실패, 툴 없이 기동: {type(e).__name__}: {e}")
        return {}, 0
