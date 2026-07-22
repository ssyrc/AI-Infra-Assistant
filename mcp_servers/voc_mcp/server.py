"""
VOC MCP - 과거 사용자/운영자 질의응답 이력에서 유사 사례와 해결 방법을 검색.
전용 DB(voc_db)를 사용한다 - Manual MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voc-mcp", stateless_http=True)


@mcp.tool()
async def search_voc(
    query: str,
    top_k: int = 5,
    department: str | None = None,
    resolved_only: bool = True,
) -> list[dict]:
    """과거 VOC(사용자/운영자 질의응답) 이력에서 유사 사례와 해결 방법을 검색한다.

    사용할 때: "예전에 이런 문제 어떻게 해결했나", 오류/증상 기반으로 선례를 찾을 때.
    쓰지 말 것: 공식 사용법·절차(→ manual.search_manual). 매뉴얼과 선례가 모두 유용할 것
      같으면 두 툴을 함께 호출해 종합한다.

    의미+키워드 하이브리드 검색이라 정확한 문구가 아니어도 된다. 답변에는 참고한 VOC 사례를
    출처로 밝힌다(확정된 정답이 아니라 '과거 사례'임을 명시).

    Args:
        query: 사용자 질문 또는 증상/오류 메시지. 예: "로그인 시 500 오류"
        top_k: 반환할 최대 건수(기본 5)
        department: 특정 부서로 한정(없으면 전체). 확실치 않으면 지정하지 않는다.
        resolved_only: True면 해결 완료 사례만(기본 True). 미해결 사례도 보려면 False.
    Returns:
        사례 리스트. 각 항목에 question, answer, department, resolved, created_at가 있다.
    """
    if not query or not query.strip():
        return []
    top_k = await clamp_top_k(top_k)
    candidate_k = await clamp_candidates(top_k * 5)
    pool = await get_pool("voc_db_dsn")

    vec = None
    try:
        vec = await embed_text(query)
    except Exception as e:  # noqa: BLE001
        print(f"[voc-mcp] 임베딩 실패, 키워드 검색으로 fallback: {type(e).__name__}: {e}")

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, question, answer, department, resolved, created_at,
                   ts_rank(tsv, plainto_tsquery('simple', $1)) AS score
            FROM voc_records
            WHERE ($2::text IS NULL OR department = $2)
              AND ($3::boolean IS FALSE OR resolved = true)
              AND tsv @@ plainto_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $4
            """,
            query, department, resolved_only, candidate_k,
        )
    else:
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
            vector_literal(vec), department, resolved_only, query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    docs = [f"{c['question']}\n{c['answer']}" for c in candidates]
    ranked = await rerank(query, docs, top_k)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        result.append(item)
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8003))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
