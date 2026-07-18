"""
Manual MCP - 사용자 가이드/매뉴얼(엑셀·PPT·워드 → 청크화된 문서) RAG 검색.
관리자 콘솔에서 발행(status='published')한 문서만 검색 대상이 된다.
전용 DB(manual_db)를 사용한다 - VOC/Command/System MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("manual-mcp", stateless_http=True)


@mcp.tool()
async def search_manual(query: str, top_k: int = 5) -> list[dict]:
    """매뉴얼/가이드 문서에서 질문과 관련된 내용을 의미 기반으로 검색한다.

    Args:
        query: 사용자 질문 또는 검색어
        top_k: 반환할 최대 청크 수 (기본 5)
    """
    vec = await embed_text(query)
    pool = await get_pool("manual_db_dsn")
    rows = await pool.fetch(
        """
        SELECT c.id, c.section_title, c.page_no, c.chunk_text,
               f.title, f.filename, f.version,
               1 - (c.embedding <=> $1::vector) AS score
        FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE f.status = 'published'
        ORDER BY c.embedding <=> $1::vector
        LIMIT $2
        """,
        vector_literal(vec),
        top_k,
    )
    return [dict(r) for r in rows]


@mcp.tool()
async def get_document(manual_file_id: int) -> list[dict]:
    """특정 매뉴얼 문서의 전체 청크를 순서대로 반환한다 (문서 전체 맥락이 필요할 때 사용).
    발행(published) 상태의 문서만 조회 가능하다.

    Args:
        manual_file_id: manual_files 테이블의 문서 ID
    """
    pool = await get_pool("manual_db_dsn")
    rows = await pool.fetch(
        """
        SELECT c.section_title, c.page_no, c.chunk_text
        FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.manual_file_id = $1 AND f.status = 'published'
        ORDER BY c.seq, c.page_no NULLS LAST, c.id
        """,
        manual_file_id,
    )
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import os as _os
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(_os.environ.get("MCP_PORT", 8001)))
