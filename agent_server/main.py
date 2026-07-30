"""
Open WebUI가 OpenAI 호환 엔드포인트로 붙는 FastAPI 앱.

세션 전략 (혼합 구조 제거):
Open WebUI는 매 요청에 대화 전체 messages를 보내므로, 이 서버는 **완전 stateless**로 동작한다.
요청마다 세션을 만들고 직전 대화 이력을 주입한 뒤 마지막 사용자 메시지를 실행하고,
응답 후 세션을 정리한다. 대화 격리가 보장되고 replica를 늘려도 세션 공유가 필요 없다.
세션 저장소는 DatabaseSessionService(Postgres)를 쓰되, 요청 종료 시 삭제해 누적을 막는다.

스트리밍:
ADK는 중간 이벤트(부분 응답/툴 호출)를 여러 번 내보내고, 텍스트가 누적된 형태로 올 수 있다.
이미 보낸 접두사를 추적해 실제 증가분(delta)만 전송한다.
"""
import os
import re
import sys
import time
import uuid
import json
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.events import Event
from google.adk.agents.run_config import RunConfig, StreamingMode

STREAMING_RUN_CONFIG = RunConfig(streaming_mode=StreamingMode.SSE)
from google.genai import types

import httpx

from contextlib import nullcontext

try:
    # 트레이스에 user_id/session_id를 붙이는 openinference 컨텍스트(있을 때만 사용).
    from openinference.instrumentation import using_attributes as _using_attributes
except Exception:  # noqa: BLE001
    _using_attributes = None

from config_store import get_config
from db import close_http_client, get_http_client
from memory_store import (
    load_context, format_memory_block, record_turns, maybe_summarize,
    list_user_memory, add_user_memory, delete_user_memory,
)
from service_hub import search_similar_voc
from chart_inline import ChartInliner, charts_base_url
from agent import build_agent, APP_NAME

MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 32000
MAX_TOTAL_CHARS = 200000

# dev 목업(mock-vllm)일 때는 그 사실이 바로 보이게 실제 모델명을 노출하고,
# 실제 vLLM(운영망 IP)로 붙으면 클라이언트(Open WebUI 등)에는 내부 모델명 대신 브랜드명을 보여준다.
MOCK_LLM_BASE_MARKER = "mock-vllm"
DISPLAY_MODEL_NAME = "AI Infra Assistant"

state: dict = {}


async def _display_model_name() -> str:
    """Open WebUI 등 클라이언트에 노출할 모델 이름. vllm_llm_model은 hot_reload 설정이라
    요청마다 새로 읽어야 설정 탭에서 바꾼 값이 재시작 없이 바로 반영된다."""
    base_url = await get_config("vllm_llm_base_url", "")
    if MOCK_LLM_BASE_MARKER in base_url:
        return await get_config("vllm_llm_model", "qwen3-32b")
    return DISPLAY_MODEL_NAME


async def _close_toolsets(toolsets: list):
    """요청 단위로 만든 MCP toolset을 정리한다(연결 누수 방지)."""
    for ts in toolsets or []:
        try:
            await ts.close()
        except Exception as e:  # noqa: BLE001
            print(f"[agent] toolset 정리 실패(무시): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 기동 시 1회: 설정/ MCP 주소 유효성 검증. 실제 실행 에이전트와 모델명은 요청마다 새로 가져온다.
    _agent, _model_name, toolsets = await build_agent()
    await _close_toolsets(toolsets)
    session_db_dsn = await get_config("agent_session_db_dsn")
    if not session_db_dsn:
        raise RuntimeError("agent_session_db_dsn이 설정되지 않았습니다.")
    state["session_service"] = DatabaseSessionService(db_url=session_db_dsn)
    try:
        yield
    finally:
        await close_http_client()


app = FastAPI(lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "model": await _display_model_name()}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": await _display_model_name(), "object": "model"}]}


def _text_of(content) -> str:
    """OpenAI 형식은 content가 문자열 또는 파트 배열일 수 있다. 텍스트만 추출한다."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def _validate(req: ChatCompletionRequest, model_name: str) -> list[tuple[str, str]]:
    """요청을 검증하고 (role, text) 목록을 돌려준다."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어 있습니다.")
    if len(req.messages) > MAX_MESSAGES:
        raise HTTPException(413, f"메시지가 너무 많습니다(최대 {MAX_MESSAGES}개).")

    if req.model and req.model != model_name:
        raise HTTPException(400, f"지원하지 않는 모델입니다: {req.model}")

    normalized: list[tuple[str, str]] = []
    total = 0
    for m in req.messages:
        text = _text_of(m.content)
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(413, f"메시지가 너무 깁니다(최대 {MAX_MESSAGE_CHARS}자).")
        total += len(text)
        normalized.append((m.role, text))
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(413, f"대화 전체 길이가 너무 깁니다(최대 {MAX_TOTAL_CHARS}자).")

    # system 메시지는 에이전트 instruction이 담당하므로 대화 이력에서 제외
    convo = [(r, t) for r, t in normalized if r in ("user", "assistant")]
    if not convo:
        raise HTTPException(400, "user 또는 assistant 메시지가 필요합니다.")
    if convo[-1][0] != "user":
        raise HTTPException(400, "마지막 메시지는 user여야 합니다.")
    if not convo[-1][1].strip():
        raise HTTPException(400, "마지막 사용자 메시지가 비어 있습니다.")
    return convo


async def _trim_history(history: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """대화 이력을 글자 수 예산 안으로 줄인다(오래된 턴부터 버림).

    Open WebUI는 대화 전체를 매 요청에 실어 보낸다. 검색 결과가 붙은 긴 답변이 몇 번
    쌓이면 이력만으로 컨텍스트가 가득 차고, 실제로 32768토큰을 넘겨
    ContextWindowExceededError가 났다. 최근 턴이 가장 중요하므로 뒤에서부터 채운다.
    """
    try:
        budget = int(await get_config("history_max_chars", "8000"))
    except (TypeError, ValueError):
        budget = 8000
    if budget <= 0:
        return history
    kept, used = [], 0
    for role, text in reversed(history):
        t = text or ""
        if used + len(t) > budget and kept:
            break
        kept.append((role, t))
        used += len(t)
    if len(kept) < len(history):
        print(f"[agent] 대화 이력 {len(history)}턴 중 최근 {len(kept)}턴만 사용"
              f"(예산 {budget}자)")
    return list(reversed(kept))


async def _create_session(user_id: str, history: list[tuple[str, str]]) -> str:
    session_id = str(uuid.uuid4())
    svc = state["session_service"]
    await svc.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    for role, text in await _trim_history(history):
        adk_role = "user" if role == "user" else "model"
        event = Event(author=adk_role,
                      content=types.Content(role=adk_role, parts=[types.Part(text=text)]))
        session = await svc.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        await svc.append_event(session=session, event=event)
    return session_id


async def _cleanup_session(user_id: str, session_id: str):
    """요청 단위 세션이므로 응답 후 삭제해 세션 테이블 누적을 막는다."""
    try:
        await state["session_service"].delete_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 세션 정리 실패(무시): {e}")


def _sse(request_id: str, model: str, delta: str, finish: bool = False) -> str:
    payload = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0,
                     "delta": {} if finish else {"content": delta},
                     "finish_reason": "stop" if finish else None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(p.text or "" for p in event.content.parts)


# 도구 이름을 그대로 보여주면 사용자에게 의미가 없고 내부 구현이 드러난다.
# 이름의 성격(검색/실행/조회)만 보고 사람 말로 바꿔서, 무엇을 하는 중인지만 알린다.
# 관리자가 콘솔에서 새 도구를 추가해도 규칙이 그대로 적용되도록 이름 매칭은 부분 문자열로 한다.
_ACTION_RULES = (
    ("manual",   "매뉴얼에서 {q} 찾는 중"),
    ("document", "문서 내용 확인하는 중"),
    ("voc",      "과거 사례에서 {q} 찾는 중"),
    ("command",  "{q} 관련 커맨드 확인하는 중"),
    ("job",      "작업 상태 확인하는 중"),
    ("gpu",      "GPU 상태 확인하는 중"),
    ("disk",     "저장공간 확인하는 중"),
    ("file",     "파일 확인하는 중"),
    ("dir",      "디렉토리 확인하는 중"),
    ("system",   "시스템 정보 확인하는 중"),
)


def _first_text_arg(args: dict) -> str:
    for v in (args or {}).values():
        if isinstance(v, str) and v.strip():
            return v.strip()[:40]
    return ""


def _action_phrase(name: str, args: dict) -> str:
    """도구 호출을 사용자에게 보여줄 한 줄 문장으로 바꾼다(도구 이름은 노출하지 않는다)."""
    low = (name or "").lower()
    q = _first_text_arg(args)
    quoted = f"'{q}'" if q else ""
    if "run" in low or "exec" in low:
        return f"{quoted} 실행하는 중" if q else "커맨드 실행하는 중"
    for key, template in _ACTION_RULES:
        if key in low:
            return template.format(q=quoted).replace("  ", " ").strip()
    if "search" in low or "find" in low:
        return f"{quoted} 검색하는 중" if q else "검색하는 중"
    return "확인하는 중"


def _result_phrase(name: str, resp) -> str:
    """도구 결과를 짧은 상태 문장으로 요약한다."""
    r = resp
    if isinstance(r, dict) and "result" in r and "exit_code" not in r and "stdout" not in r:
        r = r["result"]
    if isinstance(r, list):
        return f"{len(r)}건 찾음" if r else "찾은 내용 없음"
    if isinstance(r, dict):
        # 실행 툴이면 '어디서 누구 권한으로' 돌았는지 함께 보여준다.
        # 출력만 보고는 진짜 실행됐는지, 의도한 서버가 맞는지 알 수 없기 때문이다.
        where = ""
        if r.get("ip") or r.get("as_user"):
            where = " (" + " · ".join(str(x) for x in (r.get("ip"), r.get("as_user")) if x) + ")"
        if r.get("error"):
            return f"실패{where} — {str(r['error'])[:60]}"
        if "exit_code" in r:
            return (f"완료{where}" if r.get("exit_code") == 0
                    else f"실패{where}(종료코드 {r['exit_code']})")
        return "확인 완료"
    if r is None:
        return "찾은 내용 없음"
    text = str(r).strip()
    return (text[:50] + "…") if len(text) > 50 else (text or "완료")


def _tool_status_lines(event) -> str:
    """진행 상황을 사람이 읽는 한 줄로 만든다(도구 이름·인자 원문은 노출하지 않는다).

    답변 앞에 그대로 흘려보낸다(예전에는 <details> 블록으로 감쌌는데, 클라이언트에 따라
    태그가 그대로 보여서 걷어냈다). 사용자는 "지금 무엇을 하는 중인지"만 알게 되고
    내부 도구 구성은 드러나지 않는다.
    """
    lines = []
    for fc in (event.get_function_calls() or []):
        lines.append(f"· {_action_phrase(fc.name, fc.args)}")
    for fr in (event.get_function_responses() or []):
        lines.append(f"· {_result_phrase(fr.name, fr.response)}")
    return "\n".join(lines)


class _StreamDedup:
    """ADK 스트리밍 이벤트를 '사용자에게 새로 보낼 증가분'으로 바꾼다.

    왜 필요한가: ADK는 한 메시지를 partial 이벤트 여러 개로 흘려보낸 뒤, **같은 내용을 담은
    최종 이벤트를 한 번 더** 보낸다. 이걸 그대로 흘리면 답변이 두 번씩 출력된다.
    까다로운 점 두 가지를 모두 처리한다.
      1) partial의 text가 '델타'인 경우와 '지금까지 누적'인 경우가 둘 다 있다.
      2) 툴 호출이 끼면 메시지가 여러 개 생기고, 메시지마다 누적이 처음부터 다시 시작한다.
    그래서 partial 플래그로 메시지 경계를 잡고(최종 이벤트 = 경계), 경계마다 누적을 리셋한다.
    """

    def __init__(self):
        self.cur = ""            # 현재 메시지에서 이미 보낸 텍스트
        self.saw_partial = False
        self.full = ""           # 이번 턴 전체 텍스트(메모리 저장·완성 응답용)

    def feed(self, event) -> str:
        text = _event_text(event)
        if not text:
            return ""
        if getattr(event, "partial", False):
            self.saw_partial = True
            delta = text[len(self.cur):] if text.startswith(self.cur) else text
            self.cur += delta
        else:
            if not self.saw_partial:
                delta = text                      # partial 없이 최종만 온 메시지
            elif text.startswith(self.cur):
                delta = text[len(self.cur):]      # 대개 "" (이미 다 보냄)
            else:
                delta = ""                        # 이미 보낸 내용의 재전송 -> 버린다
            self.cur, self.saw_partial = "", False   # 메시지 경계
        self.full += delta
        return delta



# --- 차트 인라인 -------------------------------------------------------------------
# Chart MCP는 `chart://<id>` 표시자만 돌려준다(프롬프트 예산 때문). 사용자에게 내보낼 때
# 여기서 data URI로 바꿔 넣으므로, 이미지용 포트를 열거나 외부 주소를 설정할 필요가 없다.
# 대화 이력에는 표시자가 그대로 남는다(다음 요청 프롬프트가 부풀지 않게).
async def _fetch_chart_svg(chart_id: str) -> str | None:
    base = charts_base_url(await get_config("chart_mcp_url", ""))
    if not base:
        return None
    client = await get_http_client()
    r = await client.get(f"{base}/charts/{chart_id}.svg", timeout=10)
    if r.status_code != 200:
        print(f"[chart] {chart_id} 응답 {r.status_code}")
        return None
    return r.text


def _chart_inliner() -> ChartInliner:
    return ChartInliner(_fetch_chart_svg)


def _trace_ctx(user_id: str, session_id: str | None, source: str | None):
    """Langfuse 트레이스에 user_id/session_id(대화)를 붙여 사용자별로 묶이게 한다.
    openinference가 없거나 트레이싱이 꺼져 있으면 무해한 no-op이다."""
    if _using_attributes is None:
        return nullcontext()
    md = {"source": source} if source else None
    return _using_attributes(user_id=user_id or "anonymous",
                             session_id=session_id or "", metadata=md)


def _to_os_identity(raw: str) -> str:
    """OS 계정 신원으로 정규화한다.
    - 이메일 형태(user@corp.com)면 로컬파트(@ 앞)만 사용한다 -> 리눅스 계정명으로 매핑.
    - 리눅스 계정명은 소문자라 소문자로 맞춘다(Open WebUI 이메일에 대문자가 섞여 있어도 매핑되게).
    - 형식 검증/특권 계정 거부는 실행 직전 shared/ssh_exec.validate_user가 담당한다."""
    ident = (raw or "").strip()
    if "@" in ident:
        ident = ident.split("@", 1)[0].strip()
    return ident.lower()


def _caller_from_request(request: Request, req: ChatCompletionRequest) -> tuple[str, str, str]:
    """호출자 신원을 Open WebUI가 전달하는 헤더에서 읽는다.
    Open WebUI에서 ENABLE_FORWARD_USER_INFO_HEADERS=true여야 이 헤더들이 온다.
    OS 계정 매핑에 쓰려고 이메일(로컬파트)을 우선한다. Open WebUI의 User-Id는 보통 UUID라
    리눅스 계정과 맞지 않기 때문이다. body의 user 필드는 대개 비어 있어 헤더를 우선한다.
    (agent-server는 내부망에서 Open WebUI만 접근하므로 이 헤더를 신뢰한다.)"""
    h = request.headers
    raw = (h.get("x-openwebui-user-email")
           or h.get("x-openwebui-user-name")
           or h.get("x-openwebui-user-id")
           or req.user
           or "anonymous")
    user_id = _to_os_identity(raw)[:128] or "anonymous"
    role = (h.get("x-openwebui-user-role") or "").strip()   # 예: "admin" | "user"
    chat_id = h.get("x-openwebui-chat-id") or ""
    return user_id, role, chat_id


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    model_name = await _display_model_name()
    convo = _validate(req, model_name)
    user_id, user_role, chat_id = _caller_from_request(request, req)
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    history, (_, last_text) = convo[:-1], convo[-1]
    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=last_text)])

    # Open WebUI 경로도 '우리' 장기 메모리를 user_id 단위로 공유한다(외부 agent와 동일 저장소).
    # 대화 이력은 이미 messages에 있으므로 최근 턴은 주입하지 않고, 증류된 장기기억만 주입한다.
    conv = chat_id or _auto_conv(user_id)
    mem_enabled = _mem_on(await get_config("memory_enabled", "true"))
    show_tools = _mem_on(await get_config("show_tool_activity", "true"))
    extra_instruction = await _longterm_memory_block(user_id, conv, last_text) if mem_enabled else None

    # 요청 단위로 에이전트를 만들어 호출자 헤더를 MCP에 전달한다.
    # System MCP는 X-User-Id로 user_scoped 툴(예: 본인 job 조회)의 user_id를 강제 주입하고,
    # X-User-Roles로 required_roles를 검사한다(Open WebUI 역할이 그대로 전달됨).
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv,
        "X-Request-Id": request_id,
        "X-User-Roles": user_role,
    }
    agent, _model, toolsets = await build_agent(caller_headers, extra_instruction)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=state["session_service"])

    if not req.stream:
        final_text = ""
        try:
            with _trace_ctx(user_id, conv, "openwebui"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
        # 이력에는 **표시자 그대로** 저장한다(data URI가 들어가면 다음 프롬프트가 부푼다).
        _bg_persist(user_id, conv, "openwebui", last_text, final_text, mem_enabled)
        return JSONResponse({
            "id": request_id, "object": "chat.completion",
            "created": int(time.time()), "model": model_name,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": await _chart_inliner().whole(final_text)},
                         "finish_reason": "stop"}],
        })

    async def event_stream():
        dedup = _StreamDedup()
        charts = _chart_inliner()
        in_think = False
        try:
            with _trace_ctx(user_id, conv, "openwebui"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message,
                                                    run_config=STREAMING_RUN_CONFIG):
                    if await request.is_disconnected():
                        print("[agent] 클라이언트 연결 종료, 스트리밍 중단")
                        break
                    if show_tools:
                        status = _tool_status_lines(event)
                        if status:
                            in_think = True
                            yield _sse(request_id, model_name, status + "\n")
                    delta = dedup.feed(event)
                    if delta:
                        # 차트 표시자가 델타 경계에 걸쳐 쪼개져 올 수 있어 안전한 부분만 흘린다.
                        out = await charts.feed(delta)
                        if out:
                            if in_think:      # 진행 줄과 답변 사이만 한 줄 띄운다
                                yield _sse(request_id, model_name, "\n")
                                in_think = False
                            yield _sse(request_id, model_name, out)

            tail = await charts.flush()       # 붙들고 있던 꼬리 마무리
            if tail:
                if in_think:
                    yield _sse(request_id, model_name, "\n")
                    in_think = False
                yield _sse(request_id, model_name, tail)
            if in_think:
                yield _sse(request_id, model_name, "\n")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 스트리밍 오류: {e}")
            yield _sse(request_id, model_name, f"\n\n[오류가 발생했습니다: {e}]")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
            _bg_persist(user_id, conv, "openwebui", last_text, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ================================================================= Agent-to-agent API + 장기 메모리
def _mem_on(raw: str | None) -> bool:
    return (raw or "true").strip().lower() == "true"


def _auto_conv(user_id: str) -> str:
    """conversation_id가 없을 때 시간(UTC 일 단위)으로 스레드를 만든다.
    같은 날 같은 사용자의 요청은 한 대화로 이어져 최근 턴·요약이 동작한다."""
    return f"auto-{user_id}-{datetime.now(timezone.utc):%Y%m%d}"


async def _longterm_memory_block(user_id: str, conversation_id: str | None, query: str):
    """증류된 장기기억만 시스템 지시문 블록으로 반환한다(최근 턴은 주입하지 않음)."""
    try:
        tk = int(await get_config("memory_top_k", "5"))
    except (TypeError, ValueError):
        tk = 5
    ctx = await load_context(user_id, conversation_id, query, 0, tk)
    return format_memory_block(ctx["longterm"]) or None
async def _summarize_turns(turns: list[dict]) -> list[str]:
    """대화 턴들에서 '이 사용자에 대해 기억할' 사실/선호를 vLLM으로 뽑아 한 줄씩 반환한다."""
    base = await get_config("vllm_llm_base_url")
    model = await get_config("vllm_llm_model", "qwen3-32b")
    convo = "\n".join(f"{t['role']}: {t['content']}" for t in turns)[:8000]
    prompt = (
        "다음 대화에서 이 '사용자'에 대해 앞으로도 기억할 가치가 있는 사실/선호/맥락만 "
        "한국어로 간결히 3~7개 항목으로 뽑아줘. 각 항목은 한 줄로, 접두어 없이 문장만. "
        "일회성 잡담, 일반 상식, 비밀번호 같은 민감정보는 제외한다. 기억할 게 없으면 빈 줄만 출력.\n\n"
        f"대화:\n{convo}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": 400},
        )
        resp.raise_for_status()
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("-•*").strip()
        # 선두 번호(1. 2)) 제거
        while s[:1].isdigit():
            s = s[1:].lstrip(".) ").strip()
        if s:
            out.append(s)
    return out[:7]


class AgentQueryIn(BaseModel):
    user_id: str
    message: str
    conversation_id: str | None = None
    source: str | None = None
    roles: list[str] | None = None
    use_memory: bool = True
    stream: bool = False


async def _memory_context(user_id: str, conversation_id: str | None, query: str):
    """(history[(role,text)], extra_instruction|None) 반환."""
    try:
        rt = int(await get_config("memory_recent_turns", "8"))
        tk = int(await get_config("memory_top_k", "5"))
    except (TypeError, ValueError):
        rt, tk = 8, 5
    ctx = await load_context(user_id, conversation_id, query, rt, tk)
    hist = [("user" if t["role"] == "user" else "assistant", t["content"]) for t in ctx["recent"]]
    return hist, (format_memory_block(ctx["longterm"]) or None)


_bg_tasks: set = set()   # 백그라운드 태스크가 GC로 사라지지 않도록 참조를 보관한다.


def _bg_persist(user_id, conversation_id, source, message, answer, mem_enabled):
    """응답 후 백그라운드로 턴 저장 + (임계 도달 시) 요약 승격.
    메모리가 꺼져 있으면(use_memory=false 또는 memory_enabled=false) 아무것도 저장하지 않는다."""
    async def _run():
        try:
            await record_turns(user_id, conversation_id, source,
                               [("user", message), ("assistant", answer)])
            if conversation_id:
                try:
                    every = int(await get_config("memory_summarize_every", "12"))
                    ttl = int(await get_config("memory_ttl_days", "180"))
                except (TypeError, ValueError):
                    every, ttl = 12, 180
                await maybe_summarize(user_id, conversation_id, _summarize_turns, every, ttl)
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 메모리 저장/요약 실패(무시): {e}")
    if answer and mem_enabled:
        task = asyncio.create_task(_run())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


@app.post("/v1/agent/query")
async def agent_query(body: AgentQueryIn, request: Request):
    """상위 agent(예: 통합 VOC)가 AI-Infra 질문을 위임하는 엔드포인트(인증 없음, 내부망 전용).
    단일 user_id로 장기 메모리를 로드/저장하며, 이후 대화에서 참고한다."""
    if not (body.user_id or "").strip() or not (body.message or "").strip():
        raise HTTPException(400, "user_id와 message는 필수입니다.")
    user_id = _to_os_identity(body.user_id)[:128] or "anonymous"
    roles = ",".join([r.strip() for r in (body.roles or []) if r and r.strip()])
    request_id = f"agentq-{uuid.uuid4().hex[:12]}"
    model_name = await _display_model_name()

    mem_enabled = body.use_memory and _mem_on(await get_config("memory_enabled", "true"))
    show_tools = _mem_on(await get_config("show_tool_activity", "true"))
    # conversation_id가 없으면 시간(일 단위)으로 자동 부여 -> 같은 날 같은 사용자는 이어짐.
    conv = (body.conversation_id or "").strip() or (_auto_conv(user_id) if mem_enabled else None)
    history, extra_instruction = ([], None)
    if mem_enabled:
        history, extra_instruction = await _memory_context(user_id, conv, body.message)

    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=body.message)])
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv or session_id,
        "X-Request-Id": request_id,
        "X-User-Roles": roles,
    }
    agent, _model, toolsets = await build_agent(caller_headers, extra_instruction)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=state["session_service"])

    if not body.stream:
        final_text = ""
        try:
            with _trace_ctx(user_id, conv, body.source or "agent-api"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
        _bg_persist(user_id, conv, body.source, body.message, final_text, mem_enabled)
        return JSONResponse({"answer": final_text, "conversation_id": conv,
                             "request_id": request_id})

    async def event_stream():
        dedup = _StreamDedup()
        charts = _chart_inliner()
        in_think = False
        try:
            with _trace_ctx(user_id, conv, body.source or "agent-api"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message,
                                                    run_config=STREAMING_RUN_CONFIG):
                    if await request.is_disconnected():
                        break
                    if show_tools:
                        status = _tool_status_lines(event)
                        if status:
                            in_think = True
                            yield _sse(request_id, model_name, status + "\n")
                    delta = dedup.feed(event)
                    if delta:
                        # 차트 표시자가 델타 경계에 걸쳐 쪼개져 올 수 있어 안전한 부분만 흘린다.
                        out = await charts.feed(delta)
                        if out:
                            if in_think:      # 진행 줄과 답변 사이만 한 줄 띄운다
                                yield _sse(request_id, model_name, "\n")
                                in_think = False
                            yield _sse(request_id, model_name, out)
            tail = await charts.flush()       # 붙들고 있던 꼬리 마무리
            if tail:
                if in_think:
                    yield _sse(request_id, model_name, "\n")
                    in_think = False
                yield _sse(request_id, model_name, tail)
            if in_think:
                yield _sse(request_id, model_name, "\n")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            yield _sse(request_id, model_name, f"\n\n[오류가 발생했습니다: {e}]")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
            _bg_persist(user_id, conv, body.source, body.message, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- 장기 메모리 관리 (인증 없음, 내부망 전용) ---
class MemoryAddIn(BaseModel):
    content: str
    kind: str = "fact"


@app.get("/v1/memory/{user_id}")
async def memory_list(user_id: str):
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    return {"user_id": uid, "items": await list_user_memory(uid)}


@app.post("/v1/memory/{user_id}")
async def memory_add(user_id: str, body: MemoryAddIn):
    if not (body.content or "").strip():
        raise HTTPException(400, "content는 필수입니다.")
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    mid = await add_user_memory(uid, body.content.strip(), body.kind or "fact", source="manual")
    return {"id": mid}


@app.delete("/v1/memory/{user_id}")
async def memory_delete(user_id: str, memory_id: int | None = None):
    """memory_id 쿼리로 개별 삭제, 없으면 사용자 기억 전체 삭제(잊힐 권리)."""
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    deleted = await delete_user_memory(uid, memory_id)
    return {"deleted": deleted}


# ================================================================= 통합 VOC agent 연동 (guide 계약)
# 입력: voc_info + output_option / 출력: {success, answer:{content, similar_voc?}, evaluation?}
# 필수: success, (성공 시) answer.content. similar_voc/evaluation은 선택(service hub mcp 연동은 추후).
class _VocRef(BaseModel):
    id: str | None = None
    name: str | None = None


class _VocRequester(BaseModel):
    user_id: str | None = None
    user_name: str | None = None
    user_dept: str | None = None


class _VocContent(BaseModel):
    text: str | None = None
    raw_text: str | None = None


class VocInfo(BaseModel):
    voc_id: str | None = None
    voc_title: str | None = None
    voc_status: str | None = None
    voc_status_name: str | None = None
    voc_class_code: str | None = None
    voc_class_name: str | None = None
    system: _VocRef | None = None
    sub_system: _VocRef | None = None
    division: _VocRef | None = None
    campus: _VocRef | None = None
    line: _VocRef | None = None
    requester: _VocRequester | None = None
    created_at: str | None = None
    voc_content: _VocContent | None = None


class VocQueryIn(BaseModel):
    voc_info: VocInfo
    output_option: str = "markdown"   # "markdown" | "html"
    stream: bool = False              # 확장: SSE 스트리밍(가이드 기본은 비스트림 JSON)
    use_memory: bool = True


_TAG_RE = re.compile(r"<[^>]+>")


def _voc_body_text(v: VocInfo) -> str:
    """VOC 본문을 뽑는다. text 우선, 없으면 raw_text의 태그를 제거해 사용."""
    c = v.voc_content
    if not c:
        return ""
    body = (c.text or "").strip()
    if not body and c.raw_text:
        body = re.sub(r"\s+", " ", _TAG_RE.sub(" ", c.raw_text)).strip()
    return body


def _voc_message(v: VocInfo, body: str) -> str:
    parts = []
    if v.voc_title:
        parts.append(f"[VOC 제목] {v.voc_title}")
    sysname = v.system.name if v.system else None
    subname = v.sub_system.name if v.sub_system else None
    if sysname or subname:
        parts.append(f"[시스템] {sysname or '-'} / {subname or '-'}")
    if v.voc_class_name:
        parts.append(f"[분류] {v.voc_class_name}")
    if v.requester and v.requester.user_dept:
        parts.append(f"[요청 부서] {v.requester.user_dept}")
    parts.append(f"[문의 내용]\n{body}")
    return "\n".join(parts)


async def _voc_similar(v: VocInfo, query: str) -> list:
    """Service Hub MCP로 유사 VOC를 조회한다(설정/방화벽 없으면 빈 리스트).
    현재 VOC의 시스템명으로 필터해 관련도를 높인다."""
    try:
        k = int(await get_config("voc_similar_top_k", "3"))
    except (TypeError, ValueError):
        k = 3
    if k <= 0:
        return []
    system_name = v.system.name if v.system else None
    return await search_similar_voc(query, system_name, k)


def _voc_format_instruction(output_option: str) -> str:
    if (output_option or "").lower() == "html":
        return ("\n\n## 출력 형식(반드시 준수)\n답변 전체를 유효한 HTML 조각으로만 출력한다. "
                "마크다운/코드펜스(```)를 쓰지 말고, 제목은 <h2>/<h3>, 목록은 <ul><li>, "
                "표는 <table><tr><td>로 구조화하며 여는/닫는 태그를 정확히 맞춘다.")
    return ("\n\n## 출력 형식(반드시 준수)\n답변 전체를 마크다운으로만 출력한다. "
            "제목/목록/표/코드블록을 적절히 사용한다.")


@app.post("/v1/voc/query")
async def voc_query(body: VocQueryIn, request: Request):
    """통합 VOC agent가 AI-Infra 관련 VOC를 위임하는 엔드포인트(내부망 전용, 인증 없음).
    guide 계약대로 voc_info를 받아 분석 답변을 {success, answer:{content}} 형태로 돌려준다.
    output_option(markdown|html)에 맞춰 답변 형식을 강제하고, requester.user_id로 장기 메모리를 공유한다."""
    v = body.voc_info
    user_id = _to_os_identity((v.requester.user_id if v.requester else None) or "")[:128] or "anonymous"
    body_text = _voc_body_text(v)
    if not body_text:
        return JSONResponse({"success": False, "answer": None,
                             "error": "voc_content(text/raw_text)가 비어 있습니다."}, status_code=400)

    message = _voc_message(v, body_text)
    request_id = f"voc-{uuid.uuid4().hex[:12]}"
    conv = (v.voc_id or "").strip() or _auto_conv(user_id)   # VOC 단위로 대화 스레드
    mem_enabled = body.use_memory and _mem_on(await get_config("memory_enabled", "true"))

    history, extra_instruction = ([], None)
    if mem_enabled:
        history, extra_instruction = await _memory_context(user_id, conv, message)
    fmt = _voc_format_instruction(body.output_option)
    extra_instruction = (extra_instruction + fmt) if extra_instruction else fmt

    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=message)])
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv,
        "X-Request-Id": request_id,
        "X-User-Roles": "",
    }
    agent, _model, toolsets = await build_agent(caller_headers, extra_instruction)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=state["session_service"])

    # 유사 VOC 조회는 에이전트 응답과 병렬로 돌린다(지연 최소화). Service Hub 미설정 시 빈 리스트.
    similar_task = asyncio.create_task(_voc_similar(v, body_text))

    async def _collect_similar():
        try:
            return await similar_task
        except Exception:  # noqa: BLE001
            return []

    if not body.stream:
        final_text, ok = "", True
        try:
            with _trace_ctx(user_id, conv, "voc-agent"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        except Exception as e:  # noqa: BLE001
            print(f"[agent] voc_query 오류: {e}")
            ok = False
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
        similar = await _collect_similar()
        _bg_persist(user_id, conv, "voc-agent", message, final_text, mem_enabled)
        if not ok or not final_text:
            return JSONResponse({"success": False, "answer": None})
        # 외부로 나가는 본문에서는 차트 표시자를 실제 이미지로 바꿔 준다(이력은 표시자 유지).
        answer = {"content": await _chart_inliner().whole(final_text)}
        if similar:
            answer["similar_voc"] = similar
        return JSONResponse({"success": True, "answer": answer})

    async def event_stream():
        dedup = _StreamDedup()
        in_think = False
        try:
            with _trace_ctx(user_id, conv, "voc-agent"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message,
                                                    run_config=STREAMING_RUN_CONFIG):
                    if await request.is_disconnected():
                        break
                    delta = dedup.feed(event)
                    if delta:
                        yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            # 마지막에 가이드 계약 형태의 완성 envelope을 한 번 더 보낸다.
            if dedup.full:
                similar = await _collect_similar()
                answer = {"content": await _chart_inliner().whole(dedup.full)}
                if similar:
                    answer["similar_voc"] = similar
                envelope = {"success": True, "answer": answer}
            else:
                envelope = {"success": False, "answer": None}
            yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'success': False, 'answer': None, 'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            if not similar_task.done():   # 답이 비어 await를 안 한 경우 고아 방지
                similar_task.cancel()
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
            _bg_persist(user_id, conv, "voc-agent", message, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
