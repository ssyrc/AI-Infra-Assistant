"""
VOC MCP - 과거 사용자/운영자 질의응답 이력에서 유사 사례와 해결 방법을 검색.
전용 DB(voc_db)를 사용한다 - Manual MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates  # noqa: E402
from pii import mask_record  # noqa: E402
from config_store import get_config  # noqa: E402
from retrieval import ts_or_query, expand_query, has_trgm, mmr_dedup  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voc-mcp", stateless_http=True, host="0.0.0.0")

# 답변에 '사람이 시스템을 직접 확인한 흔적'이 있으면 사용자가 따라 할 수 있는 절차가 아니다.
# 이런 사례는 방법을 안내하는 대신 운영팀 접수를 안내해야 하므로 여기서 미리 분류해 준다.
# (표현이 다양해서 정확히 다 잡을 수는 없다 - 애매하면 operator로 보수적으로 분류한다.)
_OPERATOR_HINTS = (
    "확인 결과", "확인결과", "확인해보니", "확인한 결과", "점검 결과", "점검해", "조회해보니",
    "로그를 확인", "로그 확인", "서버에 이상", "장애가 있", "장비 이상", "이상이 있었",
    "재기동", "재시작 처리", "리셋", "복구", "조치했", "조치 완료", "조치하였", "처리했",
    "처리하였", "설정을 변경", "설정 변경", "권한을 부여", "권한 부여", "계정을", "쿼터를 증설",
    "증설", "할당량을 조정", "반영했", "반영하였", "적용했", "적용하였", "삭제했", "삭제하였",
    "담당자가", "운영팀에서", "관리자가", "직접 확인",
)


def classify_handling(answer: str | None) -> str:
    """"user"(사용자가 직접 해결 가능) | "operator"(운영자가 확인·조치한 건)."""
    text = (answer or "").replace(" ", "")
    for hint in _OPERATOR_HINTS:
        if hint.replace(" ", "") in text:
            return "operator"
    return "user"



@mcp.tool()
async def search_voc(
    query: str,
    top_k: int = 5,
    department: str | None = None,
    resolved_only: bool = True,
) -> list[dict]:
    """과거 VOC(사용자/운영자 질의응답) 이력에서 유사 사례와 해결 방법을 검색한다.

    사용할 때: "예전에 이런 문제 어떻게 해결했나", 오류/증상 기반으로 선례를 찾을 때.
    쓰지 말 것: 공식 사용법·절차만 물은 경우(→ 매뉴얼 검색). 매뉴얼과 선례가 모두 유용할 것
      같으면 두 툴을 함께 호출해 종합한다.

    의미+키워드 하이브리드 검색이라 정확한 문구가 아니어도 된다. 결과는 **확정된 절차가 아니라
    과거 사례**이므로 그렇게 드러나게 답한다.

    각 항목의 handled_by로 답변 방식을 정한다.
      "user" — 매뉴얼/단순 안내로 해결된 건. 그 방법을 지금 질문에 맞게 정리해 안내한다.
      "operator" — 운영자가 시스템·로그·설정을 직접 확인해 처리한 건. 사용자가 따라 할 수 있는
        절차가 아니므로 방법을 안내하지 말고, 비슷한 상황을 운영팀이 확인·조치한 사례가 있다는
        점과 함께 운영팀 접수를 안내한다.

    개인·조직 식별 정보(계정/이메일/이름/부서)는 이미 자리표시자로 치환되어 반환된다.
    자리표시자를 실제 값처럼 추측해 채우지 않는다.

    Args:
        query: 사용자 질문 또는 증상/오류 메시지. 예: "로그인 시 500 오류"
        top_k: 반환할 최대 건수(기본 5)
        department: 특정 부서로 한정(없으면 전체). 확실치 않으면 지정하지 않는다.
        resolved_only: True면 해결 완료 사례만(기본 True). 미해결 사례도 보려면 False.
    Returns:
        사례 리스트. 각 항목에 question, answer, department, resolved, created_at,
        handled_by("user"|"operator")가 있다.
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

    variants = expand_query(query)
    ts_query = ts_or_query(" ".join(variants)) or ts_or_query(query) or "''"
    use_trgm = await has_trgm(pool, "voc_db_dsn")

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, question, answer, department, resolved, created_at,
                   ts_rank(tsv, to_tsquery('simple', $1)) AS score
            FROM voc_records
            WHERE ($2::text IS NULL OR department = $2)
              AND ($3::boolean IS FALSE OR resolved = true)
              AND tsv @@ to_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $4
            """,
            ts_query, department, resolved_only, candidate_k,
        )
    elif use_trgm:
        # 3축 RRF: 벡터(의미) + 키워드(정확 일치) + 3-gram(한국어 부분 일치)
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM voc_records
                WHERE ($2::text IS NULL OR department = $2)
                  AND ($3::boolean IS FALSE OR resolved = true)
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $4)) DESC) AS rank
                FROM voc_records
                WHERE ($2::text IS NULL OR department = $2)
                  AND ($3::boolean IS FALSE OR resolved = true)
                  AND tsv @@ to_tsquery('simple', $4)
                LIMIT 50
            ),
            trgm_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY similarity(question || ' ' || answer, $5) DESC) AS rank
                FROM voc_records
                WHERE ($2::text IS NULL OR department = $2)
                  AND ($3::boolean IS FALSE OR resolved = true)
                  AND (question || ' ' || answer) % $5
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
            SELECT r.id, r.question, r.answer, r.department, r.resolved, r.created_at,
                   fused.rrf_score AS score
            FROM fused
            JOIN voc_records r ON r.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $6
            """,
            vector_literal(vec), department, resolved_only, ts_query, query, candidate_k,
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
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $4)) DESC
                ) AS rank
                FROM voc_records
                WHERE ($2::text IS NULL OR department = $2)
                  AND ($3::boolean IS FALSE OR resolved = true)
                  AND tsv @@ to_tsquery('simple', $4)
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
            vector_literal(vec), department, resolved_only, ts_query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    docs = [f"{c['question']}\n{c['answer']}" for c in candidates]
    ranked = await rerank(query, docs, top_k * 2)   # MMR로 걸러질 것을 감안해 여유 있게
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["handled_by"] = classify_handling(item.get("answer"))
        # 개인·조직 식별 정보는 에이전트에 넘기기 전에 지운다(프롬프트에 원문이 들어가지 않게).
        item = mask_record(item, ("question", "answer", "department"))
        item["rerank_score"] = rr_score
        result.append(item)

    # 사실상 같은 사례가 상위를 다 차지하지 않게 중복 제거(VOC는 유사 문의가 반복 등록된다).
    try:
        threshold = float(await get_config("dedup_similarity", "0.85"))
    except (TypeError, ValueError):
        threshold = 0.85
    return mmr_dedup(result, lambda c: f"{c['question']} {c['answer']}", top_k, threshold)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8003))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
