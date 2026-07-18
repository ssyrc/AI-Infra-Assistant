"""
Command MCP - 시스템 활용 커맨드 카탈로그 조회.
실제 실행은 System MCP가 담당하며, 여기서는 "어떤 커맨드가 있고 어떻게 쓰는지" 정보만 제공한다.
전용 DB(command_db)를 사용한다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("command-mcp", stateless_http=True)


@mcp.tool()
async def search_commands(keyword: str, top_k: int = 10) -> list[dict]:
    """이름/설명에 키워드가 포함된 시스템 커맨드를 검색한다.

    Args:
        keyword: 검색어 (예: 'scheduler', 'job')
        top_k: 반환할 최대 건수 (기본 10)
    """
    pool = await get_pool("command_db_dsn")
    rows = await pool.fetch(
        """
        SELECT name, description, usage, category
        FROM command_catalog
        WHERE name ILIKE '%' || $1 || '%' OR description ILIKE '%' || $1 || '%'
        ORDER BY name
        LIMIT $2
        """,
        keyword,
        top_k,
    )
    return [dict(r) for r in rows]


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
