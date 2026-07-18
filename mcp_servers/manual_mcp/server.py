"""
Manual MCP - 사용자 가이드/매뉴얼(엑셀·PPT·워드 → 청크화된 문서) RAG 검색.
관리자 콘솔에서 발행(status='published')한 문서만 검색 대상이 된다.
전용 DB(manual_db)를 사용한다 - VOC/Command/System MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("manual-mcp", stateless_http=True)


@mcp.tool()
async def search_manual(query: str, top_k: int = 5) -> list[dict]:
    """매뉴얼/가이드 문서에서 질문과 관련된 내용을 검색한다.
    벡터+키워드 하이브리드(RRF)로 후보를 넓게 뽑은 뒤, 리랭커로 최종 순위를 정한다.

    Args:
        query: 사용자 질문 또는 검색어
        top_k: 반환할 최대 청크 수 (기본 5)
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

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT c.id, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version,
                   ts_rank(c.tsv, plainto_tsquery('simple', $1)) AS score
            FROM manual_chunks c
            JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.tsv @@ plainto_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $2
            """,
            query, candidate_k,
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
                    ORDER BY ts_rank(c.tsv, plainto_tsquery('simple', $2)) DESC
                ) AS rank
                FROM manual_chunks c
                JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.tsv @@ plainto_tsquery('simple', $2)
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
            vector_literal(vec), query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    # 리랭커 입력에는 제목을 포함해 문맥을 준다(표시용 section_title은 그대로 유지).
    docs = [
        (f"{c['section_title']}\n{c['chunk_text']}" if c.get("section_title") else c["chunk_text"])
        for c in candidates
    ]
    ranked = await rerank(query, docs, top_k)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        result.append(item)
    return result


@mcp.tool()
async def get_document(manual_file_id: int, offset: int = 0, limit: int = 20,
                       max_chars: int = 8000) -> dict:
    """특정 매뉴얼 문서의 청크를 순서대로 반환한다(발행된 문서만).
    대형 문서를 통째로 컨텍스트에 넣지 않도록 페이지 단위로 잘라서 준다.

    Args:
        manual_file_id: manual_files 테이블의 문서 ID
        offset: 건너뛸 청크 수 (기본 0)
        limit: 가져올 최대 청크 수 (기본 20)
        max_chars: 반환 텍스트 총 길이 상한 (기본 8000자, 초과 시 잘림)
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
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8001)))
