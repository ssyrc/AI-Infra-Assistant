"""
VOC(사용자/운영자 질의응답 이력) 관리 API.
개별 등록/수정/삭제와, 엑셀(question/answer/department/resolved 컬럼) 일괄 업로드를 지원한다.
"""
import tempfile

import openpyxl
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel

from auth import require_admin
from cleaning import clean_text
from db import get_pool, embed_text, vector_literal

router = APIRouter(prefix="/api/voc", tags=["voc"])

# 사내 VOC 엑셀 표준 포맷: 4행이 헤더, 이 4개 컬럼만 쓴다(의뢰번호/클러스터/의뢰자 등은 안 씀).
_VOC_HEADER_ROW = 4
_COL_REQUEST = "의뢰내용"
_COL_ACTION_DATE = "조치일"
_COL_RESOLUTION = "처리내용"
_COL_SATISFACTION = "만족도"
# 이 값이면 제외(불만족류). 매우만족/만족/보통/빈값은 그대로 사용.
_EXCLUDED_SATISFACTION = {"불만족", "매우불만족"}


class VocIn(BaseModel):
    question: str
    answer: str
    department: str | None = None
    resolved: bool = True


@router.get("")
async def list_voc(q: str | None = None, admin: str = Depends(require_admin)):
    pool = await get_pool("voc_db_dsn")
    if q:
        rows = await pool.fetch(
            "SELECT id, question, answer, department, resolved, created_at FROM voc_records "
            "WHERE question ILIKE '%' || $1 || '%' ORDER BY created_at DESC LIMIT 200",
            q,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, question, answer, department, resolved, created_at FROM voc_records "
            "ORDER BY created_at DESC LIMIT 200"
        )
    return [dict(r) for r in rows]


@router.post("")
async def create_voc(body: VocIn, admin: str = Depends(require_admin)):
    vec = await embed_text(f"{body.question}\n{body.answer}")
    pool = await get_pool("voc_db_dsn")
    row_id = await pool.fetchval(
        """
        INSERT INTO voc_records (question, answer, department, resolved, embedding)
        VALUES ($1, $2, $3, $4, $5::vector) RETURNING id
        """,
        body.question,
        body.answer,
        body.department,
        body.resolved,
        vector_literal(vec),
    )
    return {"id": row_id}


@router.patch("/{voc_id}")
async def update_voc(voc_id: int, body: VocIn, admin: str = Depends(require_admin)):
    vec = await embed_text(f"{body.question}\n{body.answer}")
    pool = await get_pool("voc_db_dsn")
    row = await pool.fetchrow(
        """
        UPDATE voc_records SET question=$1, answer=$2, department=$3, resolved=$4, embedding=$5::vector
        WHERE id=$6 RETURNING id
        """,
        body.question,
        body.answer,
        body.department,
        body.resolved,
        vector_literal(vec),
        voc_id,
    )
    if not row:
        raise HTTPException(404, "VOC 기록을 찾을 수 없습니다.")
    return {"ok": True}


@router.delete("/{voc_id}")
async def delete_voc(voc_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("voc_db_dsn")
    await pool.execute("DELETE FROM voc_records WHERE id = $1", voc_id)
    return {"ok": True}


@router.post("/import")
async def import_voc_excel(file: UploadFile = File(...), admin: str = Depends(require_admin)):
    """사내 VOC 엑셀 표준 포맷 전용: 4행이 헤더고 의뢰내용/조치일/처리내용/만족도 컬럼만 쓴다.
    조치일·처리내용이 둘 다 있는 행만, 만족도가 불만족/매우불만족이 아닌 행만 등록한다.
    의뢰내용/처리내용은 HTML 태그만 벗기고 본문(명령어·코드 포함)은 그대로 둔다."""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    wb = openpyxl.load_workbook(tmp_path, read_only=True)
    ws = wb.active
    header_row = next(ws.iter_rows(min_row=_VOC_HEADER_ROW, max_row=_VOC_HEADER_ROW))
    header = [str(c.value).strip() if c.value else "" for c in header_row]
    required = {_COL_REQUEST, _COL_ACTION_DATE, _COL_RESOLUTION, _COL_SATISFACTION}
    if not required.issubset(set(header)):
        raise HTTPException(
            422, f"엑셀 {_VOC_HEADER_ROW}행에 {', '.join(sorted(required))} 컬럼이 모두 있어야 합니다.")

    col_idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = col_idx[name]
        return row[i] if i < len(row) else None

    pool = await get_pool("voc_db_dsn")
    inserted = 0
    skipped = 0
    for row in ws.iter_rows(min_row=_VOC_HEADER_ROW + 1, values_only=True):
        request_content = cell(row, _COL_REQUEST)
        action_date = cell(row, _COL_ACTION_DATE)
        resolution = cell(row, _COL_RESOLUTION)
        satisfaction = cell(row, _COL_SATISFACTION)

        if not request_content or not action_date or not resolution:
            skipped += 1
            continue
        if str(satisfaction).strip() in _EXCLUDED_SATISFACTION:
            skipped += 1
            continue

        question = clean_text(str(request_content))
        answer = clean_text(str(resolution))
        if not question or not answer:
            skipped += 1
            continue

        vec = await embed_text(f"{question}\n{answer}")
        await pool.execute(
            """
            INSERT INTO voc_records (question, answer, department, resolved, embedding)
            VALUES ($1, $2, $3, $4, $5::vector)
            """,
            question,
            answer,
            None,
            True,
            vector_literal(vec),
        )
        inserted += 1

    return {"inserted": inserted, "skipped": skipped}
