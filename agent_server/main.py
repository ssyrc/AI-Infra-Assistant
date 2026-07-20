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
from google.genai import types

import httpx

from contextlib import nullcontext

try:
    # 트레이스에 user_id/session_id를 붙이는 openinference 컨텍스트(있을 때만 사용).
    from openinference.instrumentation import using_attributes as _using_attributes
except Exception:  # noqa: BLE001
    _using_attributes = None

from config_store import get_config
from db import close_http_client
from memory_store import (
    load_context, format_memory_block, record_turns, maybe_summarize,
    list_user_memory, add_user_memory, delete_user_memory,
)
from service_hub import search_similar_voc
from agent import build_agent, APP_NAME

MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 32000
MAX_TOTAL_CHARS = 200000

state: dict = {}


async def _close_toolsets(toolsets: list):
    """요청 단위로 만든 MCP toolset을 정리한다(연결 누수 방지)."""
    for ts in toolsets or []:
        try:
            await ts.close()
        except Exception as e:  # noqa: BLE001
            print(f"[agent] toolset 정리 실패(무시): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 기동 시 1회: 설정/ MCP 주소 유효성 검증 + 모델명 확보. 실제 실행 에이전트는 요청마다 만든다.
    _agent, model_name, toolsets = await build_agent()
    await _close_toolsets(toolsets)
    session_db_dsn = await get_config("agent_session_db_dsn")
    if not session_db_dsn:
        raise RuntimeError("agent_session_db_dsn이 설정되지 않았습니다.")
    state["session_service"] = DatabaseSessionService(db_url=session_db_dsn)
    state["model_name"] = model_name
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
    return {"status": "ok", "model": state.get("model_name")}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": state["model_name"], "object": "model"}]}


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


def _validate(req: ChatCompletionRequest) -> list[tuple[str, str]]:
    """요청을 검증하고 (role, text) 목록을 돌려준다."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어 있습니다.")
    if len(req.messages) > MAX_MESSAGES:
        raise HTTPException(413, f"메시지가 너무 많습니다(최대 {MAX_MESSAGES}개).")

    if req.model and req.model != state["model_name"]:
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


async def _create_session(user_id: str, history: list[tuple[str, str]]) -> str:
    session_id = str(uuid.uuid4())
    svc = state["session_service"]
    await svc.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    for role, text in history:
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
    - 앞뒤 공백 제거. 이후 실제 검증은 System MCP의 pwd.getpwnam이 담당한다(없으면 실행 거부)."""
    ident = (raw or "").strip()
    if "@" in ident:
        ident = ident.split("@", 1)[0].strip()
    return ident


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
    convo = _validate(req)
    model_name = state["model_name"]
    user_id, user_role, chat_id = _caller_from_request(request, req)
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    history, (_, last_text) = convo[:-1], convo[-1]
    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=last_text)])

    # Open WebUI 경로도 '우리' 장기 메모리를 user_id 단위로 공유한다(외부 agent와 동일 저장소).
    # 대화 이력은 이미 messages에 있으므로 최근 턴은 주입하지 않고, 증류된 장기기억만 주입한다.
    conv = chat_id or _auto_conv(user_id)
    mem_enabled = _mem_on(await get_config("memory_enabled", "true"))
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
        _bg_persist(user_id, conv, "openwebui", last_text, final_text, mem_enabled)
        return JSONResponse({
            "id": request_id, "object": "chat.completion",
            "created": int(time.time()), "model": model_name,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": final_text},
                         "finish_reason": "stop"}],
        })

    async def event_stream():
        sent = ""   # 지금까지 클라이언트로 보낸 텍스트
        try:
            with _trace_ctx(user_id, conv, "openwebui"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if await request.is_disconnected():
                        print("[agent] 클라이언트 연결 종료, 스트리밍 중단")
                        break

                    text = _event_text(event)
                    if not text:
                        continue
                    # ADK 이벤트는 누적 텍스트로 올 수 있다 -> 증가분만 전송
                    if text.startswith(sent):
                        delta = text[len(sent):]
                        sent = text
                    else:
                        delta = text
                        sent += text
                    if delta:
                        yield _sse(request_id, model_name, delta)

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
            _bg_persist(user_id, conv, "openwebui", last_text, sent, mem_enabled)

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
    model_name = state["model_name"]

    mem_enabled = body.use_memory and _mem_on(await get_config("memory_enabled", "true"))
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
        sent = ""
        try:
            with _trace_ctx(user_id, conv, body.source or "agent-api"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if await request.is_disconnected():
                        break
                    text = _event_text(event)
                    if not text:
                        continue
                    if text.startswith(sent):
                        delta = text[len(sent):]
                        sent = text
                    else:
                        delta = text
                        sent += text
                    if delta:
                        yield _sse(request_id, model_name, delta)
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
            _bg_persist(user_id, conv, body.source, body.message, sent, mem_enabled)

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
        answer = {"content": final_text}
        if similar:
            answer["similar_voc"] = similar
        return JSONResponse({"success": True, "answer": answer})

    async def event_stream():
        sent = ""
        try:
            with _trace_ctx(user_id, conv, "voc-agent"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if await request.is_disconnected():
                        break
                    text = _event_text(event)
                    if not text:
                        continue
                    if text.startswith(sent):
                        delta = text[len(sent):]
                        sent = text
                    else:
                        delta = text
                        sent += text
                    if delta:
                        yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            # 마지막에 가이드 계약 형태의 완성 envelope을 한 번 더 보낸다.
            if sent:
                similar = await _collect_similar()
                answer = {"content": sent}
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
            if not similar_task.done():   # sent가 비어 await를 안 한 경우 고아 방지
                similar_task.cancel()
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
            _bg_persist(user_id, conv, "voc-agent", message, sent, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
