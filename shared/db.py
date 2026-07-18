"""
공용 DB / 임베딩 / 리랭킹 클라이언트.

- get_pool(db_key): db_key로 platform_settings에서 DSN을 읽어 MCP별 전용 DB 풀 생성.
- embed_text(): vLLM 임베딩 서버 호출. Redis 캐시가 설정돼 있으면 쿼리 임베딩을 캐시한다.
- rerank(): vLLM(또는 TEI) 리랭커 서버 호출. 설정이 없으면 원본 순서를 그대로 반환한다.
"""
import hashlib
import json

import asyncpg
import httpx

from config_store import get_config

_pools: dict[str, asyncpg.Pool] = {}
_redis = None
_redis_checked = False


async def get_pool(db_key: str) -> asyncpg.Pool:
    if db_key not in _pools:
        dsn = await get_config(db_key)
        if not dsn:
            raise RuntimeError(
                f"'{db_key}' DSN이 설정되어 있지 않습니다. 관리자 콘솔 > 설정에서 등록하세요."
            )
        _pools[db_key] = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pools[db_key]


async def _get_redis():
    """embed 캐시용 Redis. redis_url 설정이 있으면 연결, 없으면 None(캐시 비활성)."""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    url = await get_config("redis_url")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(url, encoding="utf-8", decode_responses=False)
        await _redis.ping()
    except Exception as e:  # noqa: BLE001
        print(f"[db] Redis 연결 실패, 임베딩 캐시 비활성화: {e}")
        _redis = None
    return _redis


async def embed_text(text: str) -> list[float]:
    """vLLM /v1/embeddings 호출. Redis가 있으면 캐시(키: emb:모델:sha1)."""
    model = await get_config("vllm_embed_model", "bge-m3")
    cache_key = None
    redis = await _get_redis()
    if redis is not None:
        digest = hashlib.sha1(f"{model}:{text}".encode("utf-8")).hexdigest()
        cache_key = f"emb:{digest}"
        try:
            cached = await redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:  # noqa: BLE001
            pass

    base_url = await get_config("vllm_embed_base_url")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{base_url}/embeddings", json={"model": model, "input": text})
        resp.raise_for_status()
        vec = resp.json()["data"][0]["embedding"]

    if redis is not None and cache_key:
        try:
            ttl = int(await get_config("embed_cache_ttl_seconds", "86400"))
            await redis.set(cache_key, json.dumps(vec), ex=ttl)
        except Exception:  # noqa: BLE001
            pass
    return vec


async def rerank(query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
    """리랭커 서버(vLLM score/rerank 또는 TEI /rerank)로 (원본인덱스, 점수) 상위 top_k를 반환한다.
    rerank_base_url 설정이 없으면 리랭킹을 건너뛰고 원래 순서를 그대로 반환한다."""
    if not documents:
        return []
    base_url = await get_config("rerank_base_url")
    if not base_url:
        return [(i, 0.0) for i in range(min(top_k, len(documents)))]

    model = await get_config("rerank_model", "bge-reranker-v2-m3")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/rerank",
            json={"model": model, "query": query, "documents": documents},
        )
        resp.raise_for_status()
        data = resp.json()
    # TEI/vLLM 리랭커 응답 형식 호환: {"results":[{"index":i,"relevance_score":s}, ...]}
    results = data.get("results", data if isinstance(data, list) else [])
    scored = [
        (r.get("index"), r.get("relevance_score", r.get("score", 0.0)))
        for r in results if r.get("index") is not None
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def vector_literal(vec: list[float]) -> str:
    """pgvector 쿼리 파라미터용 문자열 변환: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
