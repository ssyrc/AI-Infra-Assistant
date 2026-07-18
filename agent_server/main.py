"""
Open WebUI가 '커스텀 OpenAI-호환 엔드포인트'로 바로 붙을 수 있도록
ADK 에이전트를 /v1/chat/completions, /v1/models로 감싼 FastAPI 앱.

세션 전략: DatabaseSessionService(Postgres)를 사용해 세션을 영속화한다.
-> agent-server를 여러 대 띄워도(수평 확장) 세션이 공유된다.
   사용자(req.user) 단위로 세션을 한 번만 만들고 재사용하며, ADK Runner가
   turn마다 대화 이력을 자동으로 세션에 쌓아주므로 매 요청마다 Open WebUI가
   보내주는 messages 배열 중 '마지막 사용자 메시지'만 전달하면 된다.
"""
import os
import sys
import time
import uuid
import json
from contextlib import asynccontextmanager

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from config_store import get_config
from agent import build_agent, APP_NAME

state: dict = {}  # {"runner": Runner, "session_service": ..., "model_name": str}


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent, model_name = await build_agent()
    session_db_dsn = await get_config("agent_session_db_dsn")
    session_service = DatabaseSessionService(db_url=session_db_dsn)
    state["runner"] = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)
    state["session_service"] = session_service
    state["model_name"] = model_name
    yield


app = FastAPI(lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    user: str | None = None


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": state["model_name"], "object": "model"}]}


async def _new_session_with_history(user_id: str, messages: list["ChatMessage"]) -> str:
    """요청마다 새 세션을 만들고, 마지막(현재) 사용자 메시지를 제외한 이전 대화 이력을
    세션 이벤트로 주입한다.

    Open WebUI는 매 요청에 대화 전체 messages 배열을 보내주므로, 이 방식이면
    - 대화별 격리가 자동으로 보장되고(같은 사용자의 다른 대화가 섞이지 않음),
    - agent-server를 여러 대 띄워도 요청이 자기완결적이라 replica 간 세션 공유가 필요 없다.
    """
    session_id = str(uuid.uuid4())
    session_service = state["session_service"]
    await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

    # 마지막 메시지(현재 질문)를 제외한 이전 turn들을 세션에 미리 넣는다.
    from google.adk.events import Event
    for msg in messages[:-1]:
        if msg.role not in ("user", "assistant"):
            continue
        role = "user" if msg.role == "user" else "model"
        event = Event(
            author=role,
            content=types.Content(role=role, parts=[types.Part(text=msg.content)]),
        )
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        await session_service.append_event(session=session, event=event)
    return session_id


def _sse_chunk(request_id: str, model: str, delta: str, finish: bool = False) -> str:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {} if finish else {"content": delta},
            "finish_reason": "stop" if finish else None,
        }],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    runner = state["runner"]
    model_name = req.model or state["model_name"]
    user_id = req.user or "openwebui-user"
    session_id = await _new_session_with_history(user_id, req.messages)
    last_user_message = req.messages[-1].content
    content = types.Content(role="user", parts=[types.Part(text=last_user_message)])
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not req.stream:
        final_text = ""
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = event.content.parts[0].text or ""
        return JSONResponse({
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": final_text},
                "finish_reason": "stop",
            }],
        })

    async def event_stream():
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            if event.content and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    yield _sse_chunk(request_id, model_name, text)
        yield _sse_chunk(request_id, model_name, "", finish=True)
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
