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
import sys
import time
import uuid
import json
import asyncio
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

from config_store import get_config
from db import close_http_client
from memory_store import (
    load_context, format_memory_block, record_turns, maybe_summarize,
    list_user_memory, add_user_memory, delete_user_memory,
)
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

    # 요청 단위로 에이전트를 만들어 호출자 헤더를 MCP에 전달한다.
    # System MCP는 X-User-Id로 user_scoped 툴(예: 본인 job 조회)의 user_id를 강제 주입하고,
    # X-User-Roles로 required_roles를 검사한다(Open WebUI 역할이 그대로 전달됨).
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": chat_id or session_id,
        "X-Request-Id": request_id,
        "X-User-Roles": user_role,
    }
    agent, _model, toolsets = await build_agent(caller_headers)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=state["session_service"])

    if not req.stream:
        final_text = ""
        try:
            async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                new_message=new_message):
                if event.is_final_response():
                    final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(toolsets)
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ================================================================= Agent-to-agent API + 장기 메모리
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
        text = resp.json()["choices"][0]["message"]["content"]
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


def _bg_persist(user_id, conversation_id, source, message, answer, mem_enabled):
    """응답 후 백그라운드로 턴 저장 + (임계 도달 시) 요약 승격."""
    async def _run():
        try:
            await record_turns(user_id, conversation_id, source,
                               [("user", message), ("assistant", answer)])
            if mem_enabled and conversation_id:
                try:
                    every = int(await get_config("memory_summarize_every", "12"))
                    ttl = int(await get_config("memory_ttl_days", "180"))
                except (TypeError, ValueError):
                    every, ttl = 12, 180
                await maybe_summarize(user_id, conversation_id, _summarize_turns, every, ttl)
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 메모리 저장/요약 실패(무시): {e}")
    if answer:
        asyncio.create_task(_run())


@app.post("/v1/agent/query")
async def agent_query(body: AgentQueryIn, request: Request):
    """상위 agent(예: 통합 VOC)가 AI-Infra 질문을 위임하는 엔드포인트(인증 없음, 내부망 전용).
    단일 user_id로 장기 메모리를 로드/저장하며, 이후 대화에서 참고한다."""
    if not (body.user_id or "").strip() or not (body.message or "").strip():
        raise HTTPException(400, "user_id와 message는 필수입니다.")
    user_id = _to_os_identity(body.user_id)[:128] or "anonymous"
    roles = ",".join([r.strip() for r in (body.roles or []) if r and r.strip()])
    request_id = f"agentq-{uuid.uuid4().hex[:12]}"
    conv = (body.conversation_id or "").strip() or None
    model_name = state["model_name"]

    mem_enabled = body.use_memory and (
        (await get_config("memory_enabled", "true")) or "true").lower() == "true"
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
