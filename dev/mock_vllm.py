"""
dev 전용 mock vLLM 서버.
실제 GPU/모델 없이 관리자 콘솔 -> 임베딩 -> DB, 그리고 Open WebUI -> agent -> MCP 흐름을
끝까지 확인할 수 있도록 OpenAI 호환 엔드포인트를 흉내낸다.

- /v1/embeddings : 텍스트를 해시 기반의 결정적 1024차원 벡터로 변환 (같은 입력 -> 같은 벡터).
                    의미적 유사도는 없지만, 파이프라인/DB 저장/검색 동작 자체는 검증 가능.
- /v1/chat/completions : 마지막 사용자 메시지를 그대로 되돌려주는 에코 응답 (스트리밍 지원).
절대 운영에 쓰지 말 것.
"""
import hashlib
import json
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="mock-vllm")

DIM = 1024


class EmbedRequest(BaseModel):
    model: str | None = None
    input: str | list[str]


def _deterministic_vector(text: str) -> list[float]:
    """텍스트를 시드로 한 결정적 유사 난수 벡터. numpy 없이 표준 라이브러리만 사용."""
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    vec = []
    x = seed
    for _ in range(DIM):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        vec.append((x / 0x7FFFFFFF) * 2 - 1)  # -1..1
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


@app.post("/v1/embeddings")
async def embeddings(req: EmbedRequest):
    inputs = [req.input] if isinstance(req.input, str) else req.input
    data = [
        {"object": "embedding", "index": i, "embedding": _deterministic_vector(t)}
        for i, t in enumerate(inputs)
    ]
    return {"object": "list", "data": data, "model": req.model or "mock-embed"}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False


def _reply_text(req: ChatRequest) -> str:
    last = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    return f"[mock-llm] 다음 요청을 받았습니다: {last}"


@app.post("/v1/chat/completions")
async def chat(req: ChatRequest):
    text = _reply_text(req)
    model = req.model or "mock-llm"
    if not req.stream:
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }

    async def gen():
        for token in text.split(" "):
            chunk = {
                "id": "mock-1", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": token + " "}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-llm", "object": "model"}, {"id": "mock-embed", "object": "model"}]}
