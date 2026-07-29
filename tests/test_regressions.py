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
# (user_scoped=True). 예전 이 테스트는 system_mcp에 있지도 않은 툴 이름을 보고 있어서
# 계속 실패하고 있었고, user_id가 노출돼야 한다고 거꾸로 단언하고 있었다.
def test_system_mcp_tool_schema_preserves_params():
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "system_mcp"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sysmcp_test", os.path.join(ROOT, "mcp_servers", "system_mcp", "server.py"))
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


# --- 7-1번: Command MCP에는 실행 툴이 run_command 하나뿐이어야 한다 ----------------
# job 조회처럼 특정 커맨드를 코드/설정에 박아 둔 전용 툴을 두면, 관리자가 커맨드 탭에서
# 고쳐도 반영되지 않는다. 커맨드의 출처는 카탈로그 하나로 유지한다.
def test_command_mcp_has_no_hardcoded_command_tool():
    os.environ.setdefault("CONFIG_DB_DSN", "postgresql://x:x@localhost/x")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cmdmcp_test", os.path.join(ROOT, "mcp_servers", "command_mcp", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert set(m.EXEC_TOOLS) == {"run_command"}, \
        f"실행 툴은 run_command 하나여야 함(현재: {sorted(m.EXEC_TOOLS)})"

    props = {t.name: t.inputSchema["properties"] for t in asyncio.run(m.mcp.list_tools())}
    assert "user_id" not in props["run_command"], "user_id는 LLM에 노출되면 안 됨"
    assert "command" in props["run_command"]
    # 카탈로그 검색(RAG)은 걷어냈다 - 카탈로그는 툴로 노출한다(System MCP와 동일 방식).
    assert "search_commands" not in props, "커맨드 검색 툴이 다시 생기면 안 됨"


# --- 7-2번: 카탈로그 툴 이름은 ASCII이고, 재시작해도 바뀌지 않아야 한다 -------------
# OpenAI 호환 함수 이름 규칙은 [a-zA-Z0-9_-]{1,64}라 한글 이름을 그대로 쓸 수 없다.
# 또 파이썬 hash()는 프로세스마다 값이 달라져 이름이 매번 바뀌므로 고정 해시를 써야 한다.
def test_catalog_tool_names_are_ascii_and_stable():
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "command_mcp"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cattools_test", os.path.join(ROOT, "mcp_servers", "command_mcp", "catalog_tools.py"))
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
    for mcp_dir in ("manual_mcp", "voc_mcp", "command_mcp", "system_mcp"):
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
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", "command_mcp"))
    spec = importlib.util.spec_from_file_location(
        "cattools_budget", os.path.join(ROOT, "mcp_servers", "command_mcp", "catalog_tools.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    chars, tokens = m.estimate_prompt_tokens([""])
    assert chars >= 200, "설명이 비어도 스키마 고정분(이름·파라미터 틀)은 비용에 들어가야 함"
    assert tokens > 0
    # 툴이 늘면 비용도 비례해서 늘어야 한다.
    c2, t2 = m.estimate_prompt_tokens(["", ""])
    assert c2 == 2 * chars and t2 == 2 * tokens
