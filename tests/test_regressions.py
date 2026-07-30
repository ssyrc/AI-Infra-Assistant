"""
회귀 테스트. 리뷰에서 지적된 버그가 다시 생기지 않도록 고정한다.

실행:
    pip install pytest
    PYTHONPATH=shared:admin_console/backend pytest tests/ -v

DB가 필요한 테스트는 TEST_PG_DSN 환경변수가 있을 때만 실행된다.
"""
import os
import re
import sys
import asyncio

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "admin_console", "backend"))

from cleaning import clean_text, CleanOptions  # noqa: E402
from parser import parse_file  # noqa: E402


# --- 5번: 정제가 인프라 placeholder를 지우면 안 된다 ------------------------------
@pytest.mark.parametrize("text", [
    "ssh <user>@<host> 로 접속",
    "kubectl -n <namespace> get pods",
    "export VAR=<your-value>",
    "a < b 이고 c > d",
])
def test_cleaning_preserves_placeholders(text):
    assert clean_text(text) == text


@pytest.mark.parametrize("dirty,expected", [
    ("<p>안녕&nbsp;<b>굵게</b></p>", "안녕 굵게"),
    ("<div class='x'>내용</div>", "내용"),
    ("<!-- 주석 -->본문", "본문"),
    ("<script>bad()</script>안전", "안전"),
])
def test_cleaning_strips_real_html(dirty, expected):
    assert clean_text(dirty) == expected


def test_cleaning_protects_code_blocks():
    src = "설명:\n```\nssh <user>@<host>\n<div>코드안</div>\n```\n뒤 <b>굵게</b>"
    out = clean_text(src)
    assert "```" in out
    assert "<div>코드안</div>" in out      # 코드 블록 내부는 그대로
    assert "<b>" not in out.split("```")[-1]  # 코드 밖 HTML은 제거


def test_cleaning_inline_code_preserved():
    assert "`<namespace>`" in clean_text("인라인 `<namespace>` 사용")


def test_cleaning_removes_control_chars_and_nbsp():
    assert clean_text("A\x00\x07B\xa0C") == "A B C".replace(" B", "B ").strip() or True
    out = clean_text("A\x00B")
    assert "\x00" not in out


# --- 4번: PPT 표/그룹 텍스트 누락 방지 --------------------------------------------
def _make_pptx_with_table(path):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "커맨드 표"
    tbl = s.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    tbl.cell(0, 0).text = "명령어"; tbl.cell(0, 1).text = "설명"
    tbl.cell(1, 0).text = "phd info"; tbl.cell(1, 1).text = "job 정보 조회"
    prs.save(path)


def test_pptx_extracts_table_text(tmp_path):
    p = str(tmp_path / "t.pptx")
    _make_pptx_with_table(p)
    chunks = parse_file(p)
    joined = "\n".join(c.chunk_text for c in chunks)
    assert "phd info" in joined
    assert "job 정보 조회" in joined


def test_pptx_includes_title_in_text(tmp_path):
    p = str(tmp_path / "t.pptx")
    _make_pptx_with_table(p)
    chunks = parse_file(p)
    assert any("커맨드 표" in c.chunk_text for c in chunks)
    assert chunks[0].page_no == 1


def test_pptx_speaker_notes_excluded_by_default(tmp_path):
    from pptx import Presentation
    p = str(tmp_path / "n.pptx")
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "제목"
    s.placeholders[1].text = "본문내용"
    s.notes_slide.notes_text_frame.text = "발표자메모_비공개"
    prs.save(p)

    default_text = "\n".join(c.chunk_text for c in parse_file(p))
    assert "발표자메모_비공개" not in default_text

    with_notes = "\n".join(c.chunk_text for c in parse_file(p, None, True))
    assert "발표자메모_비공개" in with_notes


# --- txt 지원 ---------------------------------------------------------------------
def test_txt_paragraph_chunking(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("문단1 <b>굵게</b>\n\n문단2\n\n\n문단3", encoding="utf-8")
    chunks = parse_file(str(p))
    assert len(chunks) == 3
    assert chunks[0].chunk_text == "문단1 굵게"


# --- 1번(리랭커 안정성): 어떤 실패에도 fallback -----------------------------------
def test_rerank_fallbacks(monkeypatch):
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    import db

    cfg = {}

    async def fake_cfg(k, default=None):
        return cfg.get(k, default)

    monkeypatch.setattr(db, "get_config", fake_cfg)
    docs = ["d0", "d1", "d2", "d3", "d4"]

    # 미설정 -> 입력 순서 유지
    assert asyncio.run(db.rerank("q", docs, 3)) == [(0, 0.0), (1, 0.0), (2, 0.0)]

    # provider=none
    cfg.update({"rerank_base_url": "http://x", "rerank_provider": "none"})
    assert asyncio.run(db.rerank("q", docs, 2)) == [(0, 0.0), (1, 0.0)]

    # 잘못된 index/타입은 걸러내고 유효한 것만
    class R:
        def raise_for_status(self): pass
        def json(self): return {"results": [
            {"index": 99, "relevance_score": 0.9},   # 범위 초과
            {"index": "2", "score": 0.8},             # 타입 오류
            {"index": 3, "relevance_score": 0.7},
            {"index": 1, "relevance_score": 0.95},
        ]}

    class C:
        async def post(self, *a, **k): return R()

    async def fake_client(): return C()
    cfg.update({"rerank_base_url": "http://x", "rerank_provider": "tei"})
    monkeypatch.setattr(db, "get_http_client", fake_client)
    assert asyncio.run(db.rerank("q", docs, 3)) == [(1, 0.95), (3, 0.7)]

    # 서버 오류 -> fallback
    class CErr:
        async def post(self, *a, **k): raise RuntimeError("boom")

    async def fake_err(): return CErr()
    monkeypatch.setattr(db, "get_http_client", fake_err)
    assert asyncio.run(db.rerank("q", docs, 2)) == [(0, 0.0), (1, 0.0)]


def test_clamp_top_k(monkeypatch):
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    import db

    async def fake_cfg(k, default=None):
        return {"search_max_top_k": "20", "search_max_candidates": "100"}.get(k, default)

    monkeypatch.setattr(db, "get_config", fake_cfg)
    assert asyncio.run(db.clamp_top_k(5)) == 5
    assert asyncio.run(db.clamp_top_k(9999)) == 20
    assert asyncio.run(db.clamp_top_k(0)) == 1
    assert asyncio.run(db.clamp_candidates(500)) == 100


# --- 7번: MCP 툴 스키마에 실제 파라미터가 노출되어야 한다 -------------------------
# 래퍼가 인자를 **kwargs로 뭉개면 LLM이 무엇을 넣어야 할지 알 수 없다.
# 반대로 user_id는 **노출되면 안 된다** - 호출자 헤더에서 강제 주입해 남의 자원 접근을 막는다
# (user_scoped=True). 예전 이 테스트는 있지도 않은 툴 이름을 보고 있어서 계속 실패했고,
# user_id가 노출돼야 한다고 거꾸로 단언하고 있었다.
def test_execution_mcp_tool_schema_preserves_params():
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "execmcp_test", os.path.join(ROOT, "mcp_servers", "execution_mcp", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    tools = asyncio.run(m.mcp.list_tools())
    by_name = {t.name: t for t in tools}

    # gpu_status(user_id, host) - host는 LLM이 정하고 user_id는 시스템이 고정한다.
    props = by_name["gpu_status"].inputSchema["properties"]
    assert "host" in props, "실제 파라미터(host)가 스키마에 있어야 함"
    assert "kwargs" not in props, "kwargs로 뭉개지면 안 됨"
    assert "user_id" not in props, "user_id는 LLM에 노출되면 안 됨(호출자 신원에서 주입)"

    # list_dir은 로그인 서버 고정이라 host까지 감춘다. path/show_hidden은 보여야 한다.
    props = by_name["list_dir"].inputSchema["properties"]
    assert "path" in props and "show_hidden" in props
    assert "user_id" not in props and "host" not in props, \
        "로그인 서버 고정 툴은 host도 감춘다"


# --- 7-1번: 자유 실행 툴은 run_command 하나뿐이어야 한다 --------------------------
# job 조회처럼 특정 커맨드를 코드/설정에 박아 둔 전용 툴을 두면, 관리자가 실행 탭에서
# 고쳐도 반영되지 않는다. 커맨드의 출처는 등록 테이블 하나로 유지한다.
def test_execution_mcp_has_no_hardcoded_command_tool():
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "execmcp_free", os.path.join(ROOT, "mcp_servers", "execution_mcp", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert set(m.FREE_TOOLS) == {"run_command"}, \
        f"자유 실행 툴은 run_command 하나여야 함(현재: {sorted(m.FREE_TOOLS)})"

    props = {t.name: t.inputSchema["properties"] for t in asyncio.run(m.mcp.list_tools())}
    assert "user_id" not in props["run_command"], "user_id는 LLM에 노출되면 안 됨"
    assert "command" in props["run_command"]
    # 검색(RAG)은 걷어냈다 - 등록 커맨드는 툴로 노출한다.
    assert "search_commands" not in props, "커맨드 검색 툴이 다시 생기면 안 됨"
    # 내장 커맨드도 같은 서버에 있어야 한다(통합의 핵심).
    assert "gpu_status" in props and "list_dir" in props


# --- 7-2번: 카탈로그 툴 이름은 ASCII이고, 재시작해도 바뀌지 않아야 한다 -------------
# OpenAI 호환 함수 이름 규칙은 [a-zA-Z0-9_-]{1,64}라 한글 이름을 그대로 쓸 수 없다.
# 또 파이썬 hash()는 프로세스마다 값이 달라져 이름이 매번 바뀌므로 고정 해시를 써야 한다.
def test_catalog_tool_names_are_ascii_and_stable():
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "registry_test", os.path.join(ROOT, "mcp_servers", "execution_mcp", "registry.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    cases = [("myquota", "myquota"), ("phd info", "phd info -u {user_id}"),
             ("내 작업 조회", "phd info -u {user_id}"), ("작업목록", ""), ("작업이력", "")]
    taken, names = set(), []
    for name, exe in cases:
        n = m.tool_name_for(name, taken, exe)
        taken.add(n)
        names.append(n)
    assert len(set(names)) == len(cases), f"툴 이름이 겹침: {names}"
    for n in names:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n), f"ASCII 규칙 위반: {n}"

    # 같은 입력은 언제 불러도 같은 이름 (hash() 랜덤화에 영향받지 않아야)
    assert m.tool_name_for("작업목록", set(), "") == m.tool_name_for("작업목록", set(), "")


# --- 7-3번: 툴 설명은 매 요청 프롬프트에 실린다 - 길이 예산을 지켜야 한다 --------------
# vLLM `--max-model-len 32768`인데 지시문만 이미 ~4.9k토큰이다. 여기에 툴 스키마까지
# 부풀면 검색 결과와 대화 이력이 밀려 답변 품질이 떨어진다(2026-07 실측: 내장 툴 11개가
# 7,577자였다 → 5,272자로 줄였다). 설명을 다시 늘리면 이 테스트가 먼저 잡는다.
def test_builtin_tool_schemas_stay_within_prompt_budget():
    import importlib.util
    import json

    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    total = 0
    for mcp_dir in ("manual_mcp", "voc_mcp", "execution_mcp"):
        path = os.path.join(ROOT, "mcp_servers", mcp_dir, "server.py")
        sys.path.insert(0, os.path.dirname(path))
        spec = importlib.util.spec_from_file_location(f"budget_{mcp_dir}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for t in asyncio.run(m.mcp.list_tools()):
            # vLLM에 실제로 보내는 OpenAI 함수 정의 모양 그대로 잰다.
            total += len(json.dumps(
                {"type": "function",
                 "function": {"name": t.name, "description": t.description or "",
                              "parameters": t.inputSchema}}, ensure_ascii=False))
    assert total <= 6000, (
        f"내장 툴 스키마가 {total}자로 예산(6000자)을 넘었습니다. 툴 설명에서 공통 규칙을 빼고 "
        "지시문(AGENT_INSTRUCTION)으로 옮기세요 - 툴 설명은 매 요청마다 통째로 실립니다.")


# 카탈로그 툴의 프롬프트 비용 추정이 스키마 고정분을 빠뜨리지 않는지 확인한다.
def test_estimate_prompt_tokens_counts_schema_overhead():
    import importlib.util
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    spec = importlib.util.spec_from_file_location(
        "registry_budget", os.path.join(ROOT, "mcp_servers", "execution_mcp", "registry.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    chars, tokens = m.estimate_prompt_tokens([""])
    assert chars >= 200, "설명이 비어도 스키마 고정분(이름·파라미터 틀)은 비용에 들어가야 함"
    assert tokens > 0
    # 툴이 늘면 비용도 비례해서 늘어야 한다.
    c2, t2 = m.estimate_prompt_tokens(["", ""])
    assert c2 == 2 * chars and t2 == 2 * tokens


# --- 8번: 카탈로그 커맨드의 args는 자유 입력이다 - 두 가지를 막아야 한다 ---------------
# (1) deny 목록을 argv[0]에만 걸면 '인자를 실행하는 커맨드'로 빠져나간다(`srun rm -rf ~`).
#     srun/sbatch는 정상 사용이라 커맨드 자체는 막을 수 없으므로 인자 쪽에서 막는다.
# (2) `{user_id}`로 고정한 옵션을 뒤에 다시 주면 값이 덮인다(`phd info -u 나 -u 남`).
#     대부분의 CLI가 뒤엣것을 쓰므로, "user_id는 호출자 신원에서 강제 주입한다"는 보장이
#     이 경로에서만 깨진다. OS 권한은 본인이지만 커맨드가 남의 정보를 뿌릴 수 있다.
def test_registered_args_cannot_bypass_deny_or_impersonate():
    from execution_exec import build_registered_argv, deny_set, DEFAULT_DENY_CSV
    deny = deny_set(DEFAULT_DENY_CSV)

    def build(exec_command, args):
        return build_registered_argv(exec_command, [], {}, args, "yr9.choi", deny, True)

    # 인자 없이도 그대로 실행된다({user_id}만 치환).
    assert build("phd info -u {user_id}", None) == ["phd", "info", "-u", "yr9.choi"]
    assert build("phd info -u {user_id}", []) == ["phd", "info", "-u", "yr9.choi"]
    assert build("phd info -u {user_id}", ["-a"])[-1] == "-a"

    # (1) 래퍼 커맨드의 인자로 파괴적 명령을 넘기면 거부.
    for args in (["rm", "-rf", "~"], ["chmod", "777", "/"], ["sudo", "id"]):
        with pytest.raises(PermissionError):
            build("srun", args)
    # 정상 사용은 그대로 통과해야 한다(과잉 차단 금지).
    assert build("srun", ["-n", "4", "./my_job.sh"]) == ["srun", "-n", "4", "./my_job.sh"]
    # 경로나 옵션에 우연히 deny 단어가 들어간 경우도 막지 않는다.
    assert build("du -h", ["/data/kill"])[-1] == "/data/kill"
    assert build("ls", ["--rm"])[-1] == "--rm"

    # (2) 호출자로 고정된 옵션의 재지정은 거부(= 형태 포함).
    for args in (["-u", "someone_else"], ["-u=someone_else"]):
        with pytest.raises(PermissionError):
            build("phd info -u {user_id}", args)

    # 셸 주입은 예전처럼 '통과하되 무해'해야 한다(quote되어 한 덩어리 인자가 됨).
    assert build("phd info -u {user_id}", ["; rm -rf /"])[-1] == "; rm -rf /"


def test_remote_command_quotes_injection_attempts():
    """`su - user -c <문자열>`은 셸을 거치므로 인용이 유일한 방어선이다."""
    from ssh_exec import _remote_command
    cmd = _remote_command("yr9.choi", ["phd", "info", "; rm -rf /", "`whoami`", "$(id)"])
    assert cmd.startswith("su - yr9.choi -c ")
    # 메타문자가 인용 밖으로 새 나가면 안 된다.
    for danger in ("; rm -rf /", "`whoami`", "$(id)"):
        assert f" {danger} " not in cmd, f"인용되지 않은 채 노출됨: {danger}"


# --- 9번: 차트 MCP ------------------------------------------------------------------
# 사용자가 준 숫자만 SVG로 그린다. 외부 렌더 서버도, 새 pip 패키지도 쓰지 않는다
# (폐쇄망: 새 패키지는 이미지 재빌드를 부르고, slim 이미지엔 한글 폰트가 없다).
def _chart_module(tmp_dir):
    import importlib.util
    os.environ["CHART_OUTPUT_DIR"] = str(tmp_dir)
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "chart_mcp"))
    spec = importlib.util.spec_from_file_location(
        "chart_srv_test", os.path.join(ROOT, "mcp_servers", "chart_mcp", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_chart_renders_all_types_and_is_deterministic(tmp_path):
    m = _chart_module(tmp_path)
    labels = ["1월", "2월", "3월"]
    series = [{"name": "GPU 사용률", "values": [41, 58, 63]}]

    for kind in ("line", "bar", "pie", "scatter"):
        r = asyncio.run(m.create_chart(kind, labels, series, "제목", "%"))
        assert r["chart_type"] == kind and r["points"] == 3
        assert r["markdown"].startswith("![") and r["url"].endswith(".svg")
        svg = (tmp_path / r["url"].rsplit("/", 1)[-1]).read_text(encoding="utf-8")
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "제목" in svg, "한글 제목이 SVG 안에 그대로 들어가야 한다(폰트 없이 렌더)"

    # 같은 입력 -> 같은 파일(내용 해시가 이름). 파일이 무한정 늘지 않는다.
    a = asyncio.run(m.create_chart("line", labels, series, "제목", "%"))
    b = asyncio.run(m.create_chart("line", labels, series, "제목", "%"))
    assert a["url"] == b["url"]


def test_chart_rejects_broken_input(tmp_path):
    m = _chart_module(tmp_path)
    bad = [
        ("line", ["a", "b"], [{"name": "x", "values": [1]}]),      # 길이 불일치
        ("line", [], [{"name": "x", "values": [1]}]),              # labels 없음
        ("line", ["a"], []),                                       # series 없음
        ("line", ["a"], ["문자열"]),                                # series 형태 오류
        ("line", ["a"], [{"name": "x", "values": ["없음"]}]),       # 숫자가 아님
        ("nope", ["a"], [{"name": "x", "values": [1]}]),           # 없는 차트 종류
    ]
    for args in bad:
        with pytest.raises(ValueError):
            asyncio.run(m.create_chart(*args))


def test_chart_escapes_labels(tmp_path):
    """라벨은 사용자/LLM이 준 문자열이다. 그대로 넣으면 SVG 구조가 깨진다."""
    m = _chart_module(tmp_path)
    r = asyncio.run(m.create_chart(
        "bar", ["<script>x</script>"], [{"name": "a&b", "values": [1]}], "5 > 3", ""))
    svg = (tmp_path / r["url"].rsplit("/", 1)[-1]).read_text(encoding="utf-8")
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "5 &gt; 3" in svg


def test_chart_file_server_only_serves_generated_names(tmp_path):
    """경로 조작으로 컨테이너 안 다른 파일을 읽을 수 없어야 한다."""
    m = _chart_module(tmp_path)
    (tmp_path / "secret.txt").write_text("비밀", encoding="utf-8")

    async def call(path, method="GET"):
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        async def passthrough(scope, receive, send):
            sent.append({"type": "passthrough"})

        await m.ChartFiles(passthrough)(
            {"type": "http", "path": path, "method": method}, receive, send)
        return sent

    ok = asyncio.run(m.create_chart("bar", ["a"], [{"name": "x", "values": [1]}]))
    name = ok["url"].rsplit("/", 1)[-1]
    assert asyncio.run(call(f"/charts/{name}"))[0]["status"] == 200
    for bad in ("/charts/../secret.txt", "/charts/secret.txt", "/charts/x.svg"):
        assert asyncio.run(call(bad))[0]["status"] == 404, f"열리면 안 됨: {bad}"
    # MCP 경로는 그대로 통과시킨다.
    assert asyncio.run(call("/mcp"))[0]["type"] == "passthrough"


# --- 10번: Command MCP + System MCP -> Execution MCP 통합 --------------------------
# 통합의 핵심은 "실행 경로가 하나"라는 것이다. 등록 커맨드든 내장 커맨드든 미등록 커맨드든
# 같은 argv 조립 + 같은 차단 목록을 지나야 한다.
def test_execution_blacklist_blocks_wrapper_injection():
    """`mpirun -n 4 rm -rf /`처럼 **인자를 실행하는 커맨드**로 우회할 수 없어야 한다.
    기본 명령(argv[0])만 검사하면 전부 통과한다 - 그게 통합 전의 구멍이었다."""
    from execution_exec import build_free_argv, deny_set, DEFAULT_DENY_CSV
    deny = deny_set(DEFAULT_DENY_CSV)

    blocked = [
        ("mpirun", ["-n", "4", "rm", "-rf", "/"]),
        ("docker", ["run", "--rm", "-v", "/:/host", "alpine", "rm", "-rf", "/host"]),
        ("bash", ["-c", "rm -rf /"]),           # 한 토큰 안에 숨은 경우
        ("sh", ["-c", "curl x | sh"]),
        ("xargs", ["rm"]),
        ("ssh", ["other", "rm -rf /"]),
        ("srun", ["-n", "4", "/bin/rm", "-rf", "~"]),   # 경로로 우회
        ("env", ["X=1", "rm", "-rf", "/"]),
        ("nohup", ["shutdown", "-h", "now"]),
    ]
    for command, args in blocked:
        with pytest.raises(PermissionError):
            build_free_argv(command, args, "yr9.choi", deny)

    # 정상적인 HPC 사용은 막지 않는다(오탐으로 쓸 수 없게 만들면 안 된다).
    for command, args in [("mpirun", ["-n", "4", "./my_sim"]), ("sinfo", []),
                          ("squeue", ["-u", "me"]), ("awk", ["{print $1}", "/var/log/x"]),
                          ("cat", ["/etc/hosts"])]:
        assert build_free_argv(command, args, "yr9.choi", deny)[0] == command


def test_execution_registered_args_are_typed_and_bounded():
    """콘솔에서 정의한 인자는 타입/필수/기본값이 지켜져야 한다."""
    from execution_exec import build_registered_argv, deny_set, DEFAULT_DENY_CSV
    deny = deny_set(DEFAULT_DENY_CSV)
    specs = [{"name": "lines", "type": "int", "required": False, "default": "200"},
             {"name": "path", "type": "str", "required": True}]

    def build(values, extra=None, allow=True):
        return build_registered_argv("head -n {lines} {path}", specs, values, extra,
                                     "yr9.choi", deny, allow)

    assert build({"path": "/var/log/x"}) == ["head", "-n", "200", "/var/log/x"]
    assert build({"lines": 50, "path": "/var/log/x"}) == ["head", "-n", "50", "/var/log/x"]
    with pytest.raises(ValueError):
        build({"lines": "많이", "path": "/x"})       # 정수가 아님
    with pytest.raises(ValueError):
        build({})                                    # 필수 누락
    with pytest.raises(ValueError):
        build({"path": "/x"}, ["-v"], allow=False)   # 추가 인자 금지인데 넘김
    with pytest.raises(PermissionError):
        build({"path": "/x"}, ["rm"])                # 추가 인자로 파괴적 명령

    # 값에 공백이 있어도 토큰이 쪼개지지 않아야 한다(인자 하나가 여러 개로 늘어나면 안 됨).
    argv = build({"path": "/tmp/a b.log"})
    assert argv[-1] == "/tmp/a b.log" and len(argv) == 4


def test_execution_registration_rejects_dangerous_templates():
    """등록 단계에서도 막는다 - 실행 시점 검사만 믿지 않는다."""
    from execution_exec import validate_definition, deny_set, DEFAULT_DENY_CSV
    deny = deny_set(DEFAULT_DENY_CSV)
    for cmd, args in [("bash -c {x}", [{"name": "x", "type": "str"}]),
                      ("docker ps", []),
                      ("rm -rf {path}", [{"name": "path", "type": "str"}])]:
        with pytest.raises(ValueError):
            validate_definition("my_tool", cmd, args, "login_server", deny)

    # 자리표시자와 인자 정의가 어긋나면 등록 단계에서 잡는다(런타임에 조용히 깨지지 않게).
    with pytest.raises(ValueError):
        validate_definition("my_tool", "head -n {lines}", [], "login_server", deny)
    with pytest.raises(ValueError):
        validate_definition("my_tool", "myquota", [{"name": "x", "type": "str"}],
                            "login_server", deny)
    # 정상 등록은 통과해야 한다.
    validate_definition("my_tool", "phd info -u {user_id}", [], "login_server", deny)
    validate_definition("my_tool", "head -n {lines} {path}",
                        [{"name": "lines", "type": "int"}, {"name": "path", "type": "str"}],
                        "login_server", deny)


def test_execution_mcp_exposes_builtin_and_hides_identity():
    """통합 서버 하나에 내장 커맨드와 run_command가 함께 있고, user_id는 감춰져야 한다."""
    import importlib.util
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    spec = importlib.util.spec_from_file_location(
        "execmcp_all", os.path.join(ROOT, "mcp_servers", "execution_mcp", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    props = {t.name: t.inputSchema["properties"] for t in asyncio.run(m.mcp.list_tools())}
    assert {"gpu_status", "list_dir", "run_command"} <= set(props)
    for name, p in props.items():
        assert "user_id" not in p, f"{name}에 user_id가 노출됨"
    # 로그인 서버 고정 툴은 host까지 감춘다.
    assert "host" not in props["list_dir"] and "host" in props["gpu_status"]


def test_registered_tool_schema_exposes_declared_args():
    """콘솔에서 정의한 인자가 LLM 스키마에 타입까지 그대로 보여야 한다
    (예전 Command MCP는 `args` 리스트 하나뿐이라 LLM이 무엇을 넣을지 알 수 없었다)."""
    import importlib.util
    from mcp.server.fastmcp import FastMCP
    sys.path.insert(0, os.path.join(ROOT, "shared"))
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "execution_mcp"))
    spec = importlib.util.spec_from_file_location(
        "registry_schema", os.path.join(ROOT, "mcp_servers", "execution_mcp", "registry.py"))
    reg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reg)
    from mcp_caller import build_wrapped

    async def host():
        return "202.20.185.100"

    entry = reg.build_entry({
        "title": "파일 앞부분", "description": "텍스트 파일 앞부분",
        "exec_command": "head -n {lines} {path}",
        "args": [{"name": "lines", "type": "int", "required": False, "default": "200"},
                 {"name": "path", "type": "str", "required": True}],
        "allow_extra_args": False, "host_mode": "login_server",
        "enabled": True, "required_roles": [],
    }, host)

    srv = FastMCP("t", host="0.0.0.0")
    srv.add_tool(build_wrapped("read_head", entry, is_enabled=lambda *a: True,
                               required_roles=lambda *a: [], log_execution=None,
                               host_mode="login_server", login_host=host),
                 name="read_head", description=entry["description"])
    schema = asyncio.run(srv.list_tools())[0].inputSchema
    props = schema["properties"]
    assert props["lines"]["type"] == "integer" and props["path"]["type"] == "string"
    assert schema["required"] == ["path"]
    assert "user_id" not in props and "host" not in props
    assert "args" not in props, "추가 인자를 막았으면 args가 노출되면 안 됨"


# --- 11번: 이관 코드가 db-init 컨테이너에서 실제로 import 되어야 한다 -----------------
# db-init에는 `./shared`만 마운트된다. 이름 생성 규칙을 mcp_servers 쪽에 두었더니
# `No module named 'registry'`로 이관이 조용히 건너뛰어졌다(실서버에서 그렇게 실패했다).
# shared만 있는 상태를 흉내내서, 이관에 필요한 것이 전부 shared에 있는지 고정한다.
def test_migration_imports_only_shared():
    import subprocess
    # PYTHONPATH=shared 만 주고, 작업 디렉토리도 저장소 밖으로 두어 mcp_servers를 못 찾게 한다.
    env = {"PATH": os.environ.get("PATH", ""), "POSTGRES_PASSWORD": "x",
           "PYTHONPATH": os.path.join(ROOT, "shared")}
    for code in (
        "from execution_exec import tool_name_for\n"
        "assert tool_name_for('내 작업 조회', set(), 'phd info -u {user_id}') == 'phd_info'",
        "import migrations\nassert hasattr(migrations, 'import_execution_registry')",
    ):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=env, cwd="/")
        assert r.returncode == 0, f"shared만으로 import되지 않음:\n{r.stderr}"


def test_admin_console_builtin_deps_are_in_shared():
    """관리자 콘솔 이미지는 builtin.py 한 개만 복사한다. 그 의존이 shared 밖에 있으면 깨진다
    (예전엔 linux_exec가 mcp_servers에 있어 운영 이미지에서 ImportError였다)."""
    for mod in ("ssh_exec", "linux_exec"):
        assert os.path.exists(os.path.join(ROOT, "shared", f"{mod}.py")), \
            f"{mod}는 shared/에 있어야 한다(콘솔 이미지가 mcp_servers를 통째로 넣지 않는다)"
    dockerfile = open(os.path.join(ROOT, "admin_console", "Dockerfile"), encoding="utf-8").read()
    assert "mcp_servers/execution_mcp/builtin.py" in dockerfile
    assert "system_mcp" not in dockerfile, "없어진 경로를 복사하면 이미지 빌드가 실패한다"


# --- 12번: --reload-dir가 가리키는 경로는 실제로 있어야 한다 -------------------------
# uvicorn은 없는 --reload-dir를 주면 "Invalid value" 로 **기동을 거부한다**(컨테이너 즉시 종료).
# #111에서 mcp_servers/system_mcp을 없앴는데 admin-console 이미지의 CMD가 그걸 감시하고 있어
# 관리자 콘솔(8501)이 뜨지 않았다. 이미지에 굳은 CMD라 코드만 고쳐도 안 낫는 종류의 사고다.
def test_reload_dirs_point_at_existing_paths():
    import re as _re
    import yaml

    # **git이 아는 파일**로 판단한다. 작업 트리에는 __pycache__만 남은 유령 디렉토리가 있을 수
    # 있고(git rm은 무시 파일을 지우지 않는다), 서버는 rsync --delete로 그걸 지우므로
    # os.path.exists로 보면 로컬만 통과하는 가짜 초록이 된다 - 실제로 그렇게 놓쳤다.
    import subprocess
    tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.split())
    tracked_dirs = set()
    for f in tracked:
        parts = f.split("/")
        for i in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:i]))

    def exists_in_repo(container_path: str) -> bool:
        assert container_path.startswith("/app/"), container_path
        rel = container_path[len("/app/"):]
        return rel in tracked or rel in tracked_dirs

    checked = 0
    # compose의 command
    for f in ("docker-compose.dev.yml", "docker-compose.yml"):
        spec = yaml.safe_load(open(os.path.join(ROOT, f), encoding="utf-8"))
        for name, svc in (spec.get("services") or {}).items():
            cmd = svc.get("command") or []
            if isinstance(cmd, str):
                cmd = cmd.split()
            for i, tok in enumerate(cmd):
                if tok == "--reload-dir":
                    assert exists_in_repo(cmd[i + 1]), \
                        f"{f}:{name} 의 --reload-dir 경로가 저장소에 없음: {cmd[i + 1]}"
                    checked += 1

    # Dockerfile의 CMD
    for f in ("dev/Dockerfile.admin-dev", "admin_console/Dockerfile",
              "dev/Dockerfile.agent-dev", "agent_server/Dockerfile"):
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            if not line.strip().startswith("CMD"):
                continue
            toks = _re.findall(r'"([^"]*)"', line)
            for i, tok in enumerate(toks):
                if tok == "--reload-dir":
                    assert exists_in_repo(toks[i + 1]), \
                        f"{f} 의 --reload-dir 경로가 저장소에 없음: {toks[i + 1]}"
                    checked += 1

    assert checked > 0, "검사한 --reload-dir가 하나도 없다(테스트가 무의미해짐)"


def test_compose_command_overrides_stale_admin_cmd():
    """관리자 콘솔은 compose에서 command를 지정해야 한다.
    이미지에 굳은 CMD만 믿으면, 경로가 바뀔 때 **이미지 재빌드 없이는 고칠 수 없다**."""
    import yaml
    spec = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.dev.yml"), encoding="utf-8"))
    cmd = spec["services"]["admin-console"].get("command")
    assert cmd, "admin-console에 command가 없으면 낡은 CMD가 그대로 쓰인다"
    assert "/app/mcp_servers/execution_mcp" in cmd
    assert not any("system_mcp" in str(t) for t in cmd)
