"""
Manual MCP - 사용자 가이드/매뉴얼(엑셀·PPT·워드 → 청크화된 문서) RAG 검색.
관리자 콘솔에서 발행(status='published')한 문서만 검색 대상이 된다.
전용 DB(manual_db)를 사용한다 - VOC/Command/System MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402
from config_store import get_config  # noqa: E402
from retrieval import ts_or_query, expand_query, has_trgm, mmr_dedup  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("manual-mcp", stateless_http=True, host="0.0.0.0")


def _with_context(c: dict) -> str:
    """리랭커에 넘길 텍스트에 문서·섹션 제목을 앞에 붙인다(문맥 주입)."""
    head = " > ".join(x for x in (c.get("title"), c.get("section_title")) if x)
    return f"{head}\n{c['chunk_text']}" if head else c["chunk_text"]


@mcp.tool()
async def search_manual(query: str, top_k: int = 5) -> list[dict]:
    """사내 매뉴얼·가이드 문서에서 질문에 답할 근거 문단을 검색한다.

    사용할 때: 사용법·설정·절차·정책·개념 등 "문서에 적혀 있을" 질문.
    쓰지 말 것: 과거 장애 해결 사례(→ voc.search_voc), 실행 가능한 커맨드 목록
      (→ command.search_commands), 실시간 서버 상태·파일(→ system/command 실행 툴).

    정확한 키워드가 없어도 된다(의미+키워드 하이브리드 검색). 결과가 부족하면 표현을 바꿔
    다시 호출한다. 답변에는 반환된 문단의 title/section_title을 출처로 인용한다.

    Args:
        query: 자연어 질문 또는 핵심 키워드. 예: "배치 스케줄 등록 방법"
        top_k: 반환할 최대 문단 수(기본 5). 폭넓게 보려면 8~10.
    Returns:
        문단 리스트. 각 항목에 title, section_title, page_no, chunk_text, manual_file_id가 있다.
        더 넓은 맥락이 필요하면 manual_file_id로 get_document을 호출한다.
    """
    if not query or not query.strip():
        return []
    top_k = await clamp_top_k(top_k)
    candidate_k = await clamp_candidates(top_k * 5)
    pool = await get_pool("manual_db_dsn")

    # 임베딩 서버 장애 시에도 검색이 완전히 실패하지 않도록 키워드 전용으로 fallback한다.
    vec = None
    try:
        vec = await embed_text(query)
    except Exception as e:  # noqa: BLE001
        print(f"[manual-mcp] 임베딩 실패, 키워드 검색으로 fallback: {type(e).__name__}: {e}")

    # 한국어는 조사/어미 때문에 AND 질의(plainto_tsquery)로는 거의 안 잡힌다 -> OR 질의로 만든다.
    # 확장 질의(어미 제거본 등)도 함께 키워드 축에 넣어 매칭 폭을 넓힌다.
    variants = expand_query(query)
    ts_query = ts_or_query(" ".join(variants)) or ts_or_query(query)
    use_trgm = await has_trgm(pool, "manual_db_dsn")

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT c.id, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version,
                   ts_rank(c.tsv, to_tsquery('simple', $1)) AS score
            FROM manual_chunks c
            JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $2
            """,
            ts_query or "''", candidate_k,
        )
    elif use_trgm:
        # 3축 RRF: 벡터(의미) + 키워드(정확 일치) + 3-gram(한국어 부분 일치)
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(c.tsv, to_tsquery('simple', $2)) DESC) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $2)
                LIMIT 50
            ),
            trgm_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY similarity(c.chunk_text, $3) DESC) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.chunk_text % $3
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id, t.id) AS id,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0)
                       + COALESCE(1.0/(60+t.rank),0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
                FULL OUTER JOIN trgm_search t ON COALESCE(v.id, k.id) = t.id
            )
            SELECT c.id, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version, fused.rrf_score AS score
            FROM fused
            JOIN manual_chunks c ON c.id = fused.id
            JOIN manual_files f ON f.id = c.manual_file_id
            ORDER BY fused.rrf_score DESC
            LIMIT $4
            """,
            vector_literal(vec), ts_query or "''", query, candidate_k,
        )
    else:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rank
                FROM manual_chunks c
                JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> $1::vector
                LIMIT 50
            ),
            keyword_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(c.tsv, to_tsquery('simple', $2)) DESC
                ) AS rank
                FROM manual_chunks c
                JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $2)
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id,
                       COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT c.id, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version, fused.rrf_score AS score
            FROM fused
            JOIN manual_chunks c ON c.id = fused.id
            JOIN manual_files f ON f.id = c.manual_file_id
            ORDER BY fused.rrf_score DESC
            LIMIT $3
            """,
            vector_literal(vec), ts_query or "''", candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    # 리랭커 입력에 '문서 제목 > 섹션 제목'을 앞에 붙인다(Contextual retrieval).
    # 청크 본문만 보면 무엇에 대한 문서인지 알 수 없어 관련도 판단이 흐려진다.
    docs = [_with_context(c) for c in candidates]
    ranked = await rerank(query, docs, top_k * 2)   # MMR로 걸러질 것을 감안해 여유 있게
    ordered = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        ordered.append(item)

    # 거의 같은 청크가 상위를 다 차지하지 않게 중복을 걸러낸다.
    try:
        threshold = float(await get_config("dedup_similarity", "0.85"))
    except (TypeError, ValueError):
        threshold = 0.85
    return mmr_dedup(ordered, lambda c: c["chunk_text"], top_k, threshold)


@mcp.tool()
async def get_document(manual_file_id: int, offset: int = 0, limit: int = 20,
                       max_chars: int = 8000) -> dict:
    """특정 매뉴얼 문서를 순서대로 이어 읽는다(발행된 문서만).

    사용할 때: search_manual로 찾은 문단만으로 부족해 문서의 앞뒤 맥락이 더 필요할 때.
    처음부터 검색 없이 호출하지 말 것 — 반드시 search_manual로 manual_file_id를 먼저 얻는다.
    대형 문서를 통째로 넣지 않도록 페이지 단위로 잘라 주며, has_more/next_offset으로 이어 읽는다.

    Args:
        manual_file_id: search_manual 결과의 manual_file_id
        offset: 건너뛸 청크 수(기본 0). 이어 읽을 때 이전 응답의 next_offset을 넣는다.
        limit: 가져올 최대 청크 수(기본 20)
        max_chars: 반환 텍스트 총 길이 상한(기본 8000자, 초과 시 잘림)
    Returns:
        total_chunks, returned, has_more, next_offset, truncated_by_max_chars, chunks[].
    """
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 50))
    max_chars = max(500, min(int(max_chars), 20000))

    pool = await get_pool("manual_db_dsn")
    total = await pool.fetchval(
        """
        SELECT count(*) FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.manual_file_id = $1 AND f.status = 'published'
        """,
        manual_file_id,
    )
    rows = await pool.fetch(
        """
        SELECT c.seq, c.section_title, c.page_no, c.chunk_text
        FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.manual_file_id = $1 AND f.status = 'published'
        ORDER BY c.seq, c.page_no NULLS LAST, c.id
        OFFSET $2 LIMIT $3
        """,
        manual_file_id, offset, limit,
    )

    chunks, used, truncated = [], 0, False
    for r in rows:
        text = r["chunk_text"]
        if used + len(text) > max_chars:
            remain = max_chars - used
            if remain > 200:
                chunks.append({**dict(r), "chunk_text": text[:remain] + " …(잘림)"})
                used = max_chars
            truncated = True
            break
        chunks.append(dict(r))
        used += len(text)

    returned_end = offset + len(chunks)
    return {
        "total_chunks": total or 0,
        "offset": offset,
        "returned": len(chunks),
        "has_more": returned_end < (total or 0) or truncated,
        "next_offset": returned_end,
        "truncated_by_max_chars": truncated,
        "chunks": chunks,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8001))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
