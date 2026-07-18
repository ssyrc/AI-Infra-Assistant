"""
공용 DB / 임베딩 / 리랭킹 클라이언트.

- get_pool(db_key): db_key로 platform_settings에서 DSN을 읽어 MCP별 전용 DB 풀 생성.
- embed_text(): vLLM 임베딩 서버 호출. Redis 캐시가 설정돼 있으면 쿼리 임베딩을 캐시한다.
- rerank(): vLLM(또는 TEI) 리랭커 서버 호출. 설정이 없으면 원본 순서를 그대로 반환한다.
"""
import hashlib
import json
import time

import asyncpg
import httpx

from config_store import get_config

_pools: dict[str, asyncpg.Pool] = {}
_http_client: httpx.AsyncClient | None = None

_redis = None
_redis_next_retry: float = 0.0
REDIS_RETRY_INTERVAL = 60  # 연결 실패 시 이 시간 뒤 재시도(영구 비활성화 방지)


async def get_http_client() -> httpx.AsyncClient:
    """임베딩·리랭커·스케줄러 호출이 공유하는 클라이언트.
    매 호출마다 새로 만들면 TCP/TLS 핸드셰이크 비용이 반복되므로 커넥션 풀을 재사용한다."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


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
    """임베딩 캐시용 Redis. 연결 실패 시 영구 비활성화하지 않고 일정 시간 뒤 재시도한다."""
    global _redis, _redis_next_retry
    if _redis is not None:
        return _redis
    if time.time() < _redis_next_retry:
        return None

    url = await get_config("redis_url")
    if not url:
        _redis_next_retry = time.time() + REDIS_RETRY_INTERVAL
        return None
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=3)
        await client.ping()
        _redis = client
        return _redis
    except Exception as e:  # noqa: BLE001
        print(f"[db] Redis 연결 실패({REDIS_RETRY_INTERVAL}s 후 재시도): {e}")
        _redis_next_retry = time.time() + REDIS_RETRY_INTERVAL
        return None


async def _cache_key(text: str, model: str) -> str:
    """캐시 키에 임베딩 서버·모델·차원·정제 정책 버전을 모두 포함한다.
    (모델이나 정제 정책이 바뀌면 자동으로 캐시가 무효화되도록)"""
    server = await get_config("vllm_embed_base_url", "")
    dim = await get_config("embed_dim", "1024")
    policy = await get_config("clean_policy_version", "1")
    raw = f"{server}|{model}|{dim}|{policy}|{text}"
    return "emb:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def embed_text(text: str) -> list[float]:
    """vLLM /v1/embeddings 호출. Redis가 있으면 캐시한다."""
    model = await get_config("vllm_embed_model", "bge-m3")
    redis = await _get_redis()
    key = None
    if redis is not None:
        key = await _cache_key(text, model)
        try:
            cached = await redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:  # noqa: BLE001
            print(f"[db] 캐시 조회 실패(무시): {e}")

    base_url = await get_config("vllm_embed_base_url")
    if not base_url:
        raise RuntimeError("vllm_embed_base_url이 설정되지 않았습니다. 관리자 콘솔 > 설정에서 등록하세요.")
    client = await get_http_client()
    resp = await client.post(
        f"{base_url.rstrip('/')}/embeddings", json={"model": model, "input": text}
    )
    resp.raise_for_status()
    vec = resp.json()["data"][0]["embedding"]

    if redis is not None and key:
        try:
            ttl = int(await get_config("embed_cache_ttl_seconds", "86400"))
            await redis.set(key, json.dumps(vec), ex=ttl)
        except Exception as e:  # noqa: BLE001
            print(f"[db] 캐시 저장 실패(무시): {e}")
    return vec


async def rerank(query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
    """리랭커로 (원본인덱스, 점수) 상위 top_k를 반환한다.

    안정성 원칙: 리랭킹은 '품질 향상'이지 '필수 경로'가 아니다.
    미설정·타임아웃·오류·형식 불일치 등 어떤 문제가 생겨도 예외를 던지지 않고
    입력 순서(=RRF 순위) 상위 top_k로 fallback한다.
    """
    if not documents:
        return []
    fallback = [(i, 0.0) for i in range(min(top_k, len(documents)))]

    base_url = await get_config("rerank_base_url")
    provider = (await get_config("rerank_provider", "tei") or "tei").lower()
    if not base_url or provider == "none":
        return fallback

    model = await get_config("rerank_model", "bge-reranker-v2-m3")
    try:
        timeout = float(await get_config("rerank_timeout_seconds", "5"))
    except (TypeError, ValueError):
        timeout = 5.0

    try:
        client = await get_http_client()
        if provider == "vllm":
            # vLLM score/rerank API
            url = f"{base_url.rstrip('/')}/rerank"
            payload = {"model": model, "query": query, "documents": documents}
        else:
            # TEI(Text Embeddings Inference) rerank API
            url = f"{base_url.rstrip('/')}/rerank"
            payload = {"query": query, "texts": documents, "raw_scores": False}

        resp = await client.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[rerank] 실패, RRF 순위로 fallback: {type(e).__name__}: {e}")
        return fallback

    # 응답 형식 호환: {"results":[{index, relevance_score}]} 또는 [{index, score}]
    if isinstance(data, dict):
        raw = data.get("results", [])
    elif isinstance(data, list):
        raw = data
    else:
        return fallback

    scored: list[tuple[int, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        # index 타입·범위 검증 (잘못된 응답으로 IndexError가 나지 않도록)
        if not isinstance(idx, int) or not (0 <= idx < len(documents)):
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        scored.append((idx, score))

    if not scored:
        return fallback
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def vector_literal(vec: list[float]) -> str:
    """pgvector 쿼리 파라미터용 문자열 변환: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


async def clamp_top_k(top_k: int) -> int:
    """LLM이 비정상적으로 큰 top_k를 넘겨도 DB/컨텍스트가 폭발하지 않도록 상한을 건다."""
    try:
        max_k = int(await get_config("search_max_top_k", "20"))
    except (TypeError, ValueError):
        max_k = 20
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 5
    return max(1, min(k, max_k))


async def clamp_candidates(candidate_k: int) -> int:
    try:
        max_c = int(await get_config("search_max_candidates", "100"))
    except (TypeError, ValueError):
        max_c = 100
    return max(1, min(int(candidate_k), max_c))
