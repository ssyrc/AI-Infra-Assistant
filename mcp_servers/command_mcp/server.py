"""
Command MCP - 시스템 활용 커맨드 카탈로그 조회.
실제 실행은 System MCP가 담당하며, 여기서는 "어떤 커맨드가 있고 어떻게 쓰는지" 정보만 제공한다.
전용 DB(command_db)를 사용한다.

검색은 벡터+키워드 하이브리드(RRF)로 후보를 뽑고 리랭커로 순위를 정한다. 따라서 사용자가
정확한 커맨드명을 몰라도 "무엇을 하고 싶은지" 설명형으로 물으면 의미상 가까운 커맨드를 찾는다.
임베딩 서버 장애 시에는 키워드(부분일치/FTS) 검색으로 자동 fallback한다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("command-mcp", stateless_http=True)


@mcp.tool()
async def search_commands(query: str, top_k: int = 10, category: str | None = None) -> list[dict]:
    """하고 싶은 작업을 설명하면 의미상 가까운 시스템 커맨드를 찾아 준다.

    정확한 커맨드명이 아니어도 된다. 예) "작업이 언제 실행되는지 확인", "스케줄 등록".

    Args:
        query: 하고 싶은 작업 설명 또는 검색어
        top_k: 반환할 최대 건수 (기본 10)
        category: 특정 카테고리로 필터링 (없으면 전체)
    """
    if not query or not query.strip():
        return []
    top_k = await clamp_top_k(top_k)
    candidate_k = await clamp_candidates(top_k * 5)
    pool = await get_pool("command_db_dsn")

    vec = None
    try:
        vec = await embed_text(query)
    except Exception as e:  # noqa: BLE001
        print(f"[command-mcp] 임베딩 실패, 키워드 검색으로 fallback: {type(e).__name__}: {e}")

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, name, description, usage, category,
                   ts_rank(tsv, plainto_tsquery('simple', $1)) AS score
            FROM command_catalog
            WHERE ($2::text IS NULL OR category = $2)
              AND (tsv @@ plainto_tsquery('simple', $1)
                   OR name ILIKE '%' || $1 || '%'
                   OR description ILIKE '%' || $1 || '%')
            ORDER BY score DESC
            LIMIT $3
            """,
            query, category, candidate_k,
        )
    else:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM command_catalog
                WHERE ($2::text IS NULL OR category = $2)
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, plainto_tsquery('simple', $3)) DESC
                ) AS rank
                FROM command_catalog
                WHERE ($2::text IS NULL OR category = $2)
                  AND tsv @@ plainto_tsquery('simple', $3)
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id,
                       COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT c.id, c.name, c.description, c.usage, c.category,
                   fused.rrf_score AS score
            FROM fused
            JOIN command_catalog c ON c.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $4
            """,
            vector_literal(vec), category, query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    docs = [f"{c['name']}\n{c['description']}" for c in candidates]
    ranked = await rerank(query, docs, top_k)
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        result.append(item)
    return result


@mcp.tool()
async def get_command_detail(name: str) -> dict | None:
    """특정 커맨드의 상세 사용법을 반환한다.

    Args:
        name: command_catalog.name 값
    """
    pool = await get_pool("command_db_dsn")
    row = await pool.fetchrow(
        "SELECT name, description, usage, category FROM command_catalog WHERE name = $1",
        name,
    )
    return dict(row) if row else None


if __name__ == "__main__":
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8002)))
