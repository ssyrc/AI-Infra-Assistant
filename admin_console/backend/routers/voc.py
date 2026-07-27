"""
VOC(사용자/운영자 질의응답 이력) 관리 API.
개별 등록/수정/삭제와, 엑셀(question/answer/department/resolved 컬럼) 일괄 업로드를 지원한다.
"""
import tempfile

import openpyxl
from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException
from pydantic import BaseModel

from auth import require_admin
from cleaning import clean_text
from db import get_pool, embed_text, vector_literal
from server_files import read_upload_or_server_file

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


def _header_row(ws, row_num: int) -> list[str]:
    row = next(ws.iter_rows(min_row=row_num, max_row=row_num))
    return [str(c.value).strip() if c.value else "" for c in row]


async def _insert_voc(pool, question: str, answer: str, department: str | None, resolved: bool) -> bool:
    if not question or not answer:
        return False
    vec = await embed_text(f"{question}\n{answer}")
    await pool.execute(
        """
        INSERT INTO voc_records (question, answer, department, resolved, embedding)
        VALUES ($1, $2, $3, $4, $5::vector)
        """,
        question, answer, department, resolved, vector_literal(vec),
    )
    return True


async def _import_raw_format(ws, header: list[str], pool) -> tuple[int, int]:
    """사내 VOC 표준 엑셀: 4행 헤더, 의뢰내용/조치일/처리내용/만족도만 사용."""
    col_idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = col_idx[name]
        return row[i] if i < len(row) else None

    inserted = skipped = 0
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
        if await _insert_voc(pool, question, answer, None, True):
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


async def _import_simple_format(ws, header: list[str], pool) -> tuple[int, int]:
    """1행 헤더 Question/Answer(대소문자 무관) + department/resolved(선택), 2행부터 데이터.
    이미 정제된 텍스트로 취급하되 혹시 남은 HTML은 안전하게 걷어낸다."""
    lower_idx = {h.lower(): i for i, h in enumerate(header)}
    q_idx, a_idx = lower_idx["question"], lower_idx["answer"]
    dept_idx = lower_idx.get("department")
    resolved_idx = lower_idx.get("resolved")

    def cell(row, idx):
        return row[idx] if idx is not None and idx < len(row) else None

    inserted = skipped = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        question_raw, answer_raw = cell(row, q_idx), cell(row, a_idx)
        if not question_raw or not answer_raw:
            skipped += 1
            continue
        department = str(cell(row, dept_idx)) if cell(row, dept_idx) else None
        resolved_val = cell(row, resolved_idx)
        resolved = (
            str(resolved_val).strip().upper() not in ("FALSE", "0", "N", "NO")
            if resolved_val is not None else True
        )

        question = clean_text(str(question_raw))
        answer = clean_text(str(answer_raw))
        if await _insert_voc(pool, question, answer, department, resolved):
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


@router.post("/import")
async def import_voc_excel(
    file: UploadFile | None = File(None),
    server_path: str | None = Form(None),
    admin: str = Depends(require_admin),
):
    """엑셀 형식을 자동으로 인식해서 등록한다. 지원하는 두 형식:
    (1) 1행 헤더 Question/Answer(대소문자 무관, department/resolved 선택) — 이미 정제된 데이터용.
    (2) 사내 VOC 표준 포맷 — 4행 헤더(의뢰내용/조치일/처리내용/만족도), 조치일·처리내용 있는 행만,
        만족도 불만족/매우불만족 제외, 본문은 HTML 태그만 벗기고 그대로 보존."""
    _, content, _ = await read_upload_or_server_file(file, server_path, {".xlsx"})
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    wb = openpyxl.load_workbook(tmp_path, read_only=True)
    ws = wb.active
    pool = await get_pool("voc_db_dsn")

    row1 = _header_row(ws, 1)
    if {"question", "answer"}.issubset({h.lower() for h in row1}):
        inserted, skipped = await _import_simple_format(ws, row1, pool)
    elif ws.max_row >= _VOC_HEADER_ROW and {
        _COL_REQUEST, _COL_ACTION_DATE, _COL_RESOLUTION, _COL_SATISFACTION
    }.issubset(set(_header_row(ws, _VOC_HEADER_ROW))):
        inserted, skipped = await _import_raw_format(ws, _header_row(ws, _VOC_HEADER_ROW), pool)
    else:
        raise HTTPException(
            422,
            "엑셀 형식을 인식하지 못했습니다. 지원 형식: "
            "(1) 1행 헤더 Question/Answer, 2행부터 데이터, 또는 "
            f"(2) {_VOC_HEADER_ROW}행 헤더에 {_COL_REQUEST}/{_COL_ACTION_DATE}/"
            f"{_COL_RESOLUTION}/{_COL_SATISFACTION} 컬럼이 모두 있는 사내 표준 포맷.",
        )

    return {"inserted": inserted, "skipped": skipped}
