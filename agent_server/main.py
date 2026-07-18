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


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent, model_name = await build_agent()
    session_db_dsn = await get_config("agent_session_db_dsn")
    if not session_db_dsn:
        raise RuntimeError("agent_session_db_dsn이 설정되지 않았습니다.")
    session_service = DatabaseSessionService(db_url=session_db_dsn)
    state["runner"] = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)
    state["session_service"] = session_service
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


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    convo = _validate(req)
    runner = state["runner"]
    model_name = state["model_name"]
    user_id = (req.user or "openwebui-user")[:128]
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    history, (_, last_text) = convo[:-1], convo[-1]
    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=last_text)])

    if not req.stream:
        final_text = ""
        try:
            async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                new_message=new_message):
                if event.is_final_response():
                    final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")
