"""
VOC MCP - 과거 사용자/운영자 질의응답 이력에서 유사 사례와 해결 방법을 검색.
전용 DB(voc_db)를 사용한다 - Manual MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal  # noqa: E402

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
    rows = await pool.fetch(
        """
        SELECT id, question, answer, department, resolved, created_at,
               1 - (embedding <=> $1::vector) AS score
        FROM voc_records
        WHERE ($2::text IS NULL OR department = $2)
          AND ($3::boolean IS FALSE OR resolved = true)
        ORDER BY embedding <=> $1::vector
        LIMIT $4
        """,
        vector_literal(vec),
        department,
        resolved_only,
        top_k,
    )
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8003)))
