"""
VOC MCP - 과거 사용자/운영자 질의응답 이력에서 유사 사례와 해결 방법을 검색.
전용 DB(voc_db)를 사용한다 - Manual MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voc-mcp", stateless_http=True)


@mcp.tool()
async def search_voc(
    query: str,
    top_k: int = 5,
    department: str | None = None,
    resolved_only: bool = True,
) -> list[dict]:
    """과거 VOC(질문/답변) 이력에서 현재 질문과 유사한 케이스와 해결 방법을 검색한다.

    Args:
        query: 사용자 질문
        top_k: 반환할 최대 건수 (기본 5)
        department: 특정 부서로 필터링 (없으면 전체)
        resolved_only: True면 해결 완료된 케이스만 검색 (기본 True)
    """
    vec = await embed_text(query)
    pool = await get_pool("voc_db_dsn")
    candidate_k = max(top_k * 5, 20)
    rows = await pool.fetch(
        """
        WITH vector_search AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
            FROM voc_records
            WHERE ($2::text IS NULL OR department = $2)
              AND ($3::boolean IS FALSE OR resolved = true)
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 50
        ),
        keyword_search AS (
            SELECT id, ROW_NUMBER() OVER (
                ORDER BY ts_rank(tsv, plainto_tsquery('simple', $4)) DESC
            ) AS rank
            FROM voc_records
            WHERE ($2::text IS NULL OR department = $2)
              AND ($3::boolean IS FALSE OR resolved = true)
              AND tsv @@ plainto_tsquery('simple', $4)
            LIMIT 50
        ),
        fused AS (
            SELECT COALESCE(v.id, k.id) AS id,
                   COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
            FROM vector_search v
            FULL OUTER JOIN keyword_search k ON v.id = k.id
        )
        SELECT r.id, r.question, r.answer, r.department, r.resolved, r.created_at,
               fused.rrf_score AS score
        FROM fused
        JOIN voc_records r ON r.id = fused.id
        ORDER BY fused.rrf_score DESC
        LIMIT $5
        """,
        vector_literal(vec),
        department,
        resolved_only,
        query,
        candidate_k,
    )
    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    # 리랭킹: 질문+답변을 문서로 사용
    docs = [f"{c['question']}\n{c['answer']}" for c in candidates]
    ranked = await rerank(query, docs, top_k)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        result.append(item)
    return result


if __name__ == "__main__":
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8003)))
