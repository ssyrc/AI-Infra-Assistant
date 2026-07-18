"""
공용 DB / 임베딩 클라이언트.

- get_pool(db_key): db_key(예: 'manual_db_dsn', 'voc_db_dsn')로 platform_settings에서
  DSN을 읽어 해당 DB 전용 커넥션 풀을 만든다. MCP마다 완전히 분리된 DB를 쓴다.
  (풀은 프로세스당 1회만 생성 -> DSN을 관리자 콘솔에서 바꾸면 해당 서비스 재시작 필요)
- embed_text(): vLLM 임베딩 서버 주소/모델명을 매 호출마다 config_store에서 조회하므로
  관리자 콘솔에서 주소를 바꾸면 재시작 없이 곧바로 반영된다.
"""
import asyncpg
import httpx

from config_store import get_config

_pools: dict[str, asyncpg.Pool] = {}


async def get_pool(db_key: str) -> asyncpg.Pool:
    if db_key not in _pools:
        dsn = await get_config(db_key)
        if not dsn:
            raise RuntimeError(
                f"'{db_key}' DSN이 설정되어 있지 않습니다. 관리자 콘솔 > 설정에서 등록하세요."
            )
        _pools[db_key] = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pools[db_key]


async def embed_text(text: str) -> list[float]:
    """vLLM OpenAI 호환 /v1/embeddings 엔드포인트 호출 (주소는 매번 config_store에서 조회)."""
    base_url = await get_config("vllm_embed_base_url")
    model = await get_config("vllm_embed_model", "bge-m3")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/embeddings",
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def vector_literal(vec: list[float]) -> str:
    """pgvector 쿼리 파라미터용 문자열 변환: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
