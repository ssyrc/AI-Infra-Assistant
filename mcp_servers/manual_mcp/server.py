"""
Manual MCP - 사용자 가이드/매뉴얼(엑셀·PPT·워드 → 청크화된 문서) RAG 검색.
관리자 콘솔에서 발행(status='published')한 문서만 검색 대상이 된다.
전용 DB(manual_db)를 사용한다 - VOC/Command/System MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from manual_search import search_manual_chunks  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("manual-mcp", stateless_http=True, host="0.0.0.0")


@mcp.tool()
async def search_manual(query: str, top_k: int = 5) -> list[dict]:
    """사내 매뉴얼·가이드 문서에서 질문에 답할 근거 문단을 검색한다.

    사용할 때: 사용법·설정·절차·정책·개념 등 "문서에 적혀 있을" 질문.
    쓰지 말 것: 과거 장애 해결 사례(→ voc.search_voc), 실행 가능한 커맨드 목록
      (→ command.search_commands), 실시간 서버 상태·파일(→ system/command 실행 툴).

    정확한 키워드가 없어도 된다(의미+키워드+3gram 하이브리드 검색). 결과가 부족하면 표현을
    바꿔 다시 호출한다. 반환된 chunk_text에는 앞뒤 문단이 함께 붙어 있어(절차 문서에서 중간
    단계가 빠지지 않도록) 그 범위 안에서 답하면 된다.

    Args:
        query: 자연어 질문 또는 핵심 키워드. 예: "배치 스케줄 등록 방법"
        top_k: 반환할 최대 문단 수(기본 5). 폭넓게 보려면 8~10.
    Returns:
        문단 리스트. 각 항목에 title, section_title, page_no, chunk_text, manual_file_id,
        reference_path가 있다. reference_path는 이 문서가 실제로 있는 위치(포탈 경로)이므로,
        문서를 참고하라고 안내할 때는 문서 제목만 말하지 말고 이 경로를 그대로 적는다.
        더 넓은 맥락이 필요하면 manual_file_id로 get_document을 호출한다.
    """
    _mode, results = await search_manual_chunks(query, top_k, with_neighbors=True)
    return results


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
