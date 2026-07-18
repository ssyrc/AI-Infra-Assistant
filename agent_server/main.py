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

from config_store import get_config
from db import close_http_client
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


def _caller_from_request(request: Request, req: ChatCompletionRequest) -> tuple[str, str, str]:
    """호출자 신원을 Open WebUI가 전달하는 헤더에서 읽는다.
    Open WebUI에서 ENABLE_FORWARD_USER_INFO_HEADERS=true여야 이 헤더들이 온다.
    body의 user 필드는 Open WebUI가 대개 채우지 않으므로 헤더를 우선한다.
    (agent-server는 내부망에서 Open WebUI만 접근하므로 이 헤더를 신뢰한다.)"""
    h = request.headers
    user_id = (h.get("x-openwebui-user-id")
               or h.get("x-openwebui-user-email")
               or req.user
               or "anonymous")
    role = h.get("x-openwebui-user-role") or ""   # 예: "admin" | "user"
    chat_id = h.get("x-openwebui-chat-id") or ""
    return user_id[:128], role.strip(), chat_id


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
