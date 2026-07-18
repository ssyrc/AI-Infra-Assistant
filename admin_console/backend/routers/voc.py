"""
VOC(사용자/운영자 질의응답 이력) 관리 API.
개별 등록/수정/삭제와, 엑셀(question/answer/department/resolved 컬럼) 일괄 업로드를 지원한다.
"""
import tempfile

import openpyxl
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel

from auth import require_admin
from db import get_pool, embed_text, vector_literal

router = APIRouter(prefix="/api/voc", tags=["voc"])


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
    """엑셀 헤더: question, answer, department(선택), resolved(선택, TRUE/FALSE)"""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    wb = openpyxl.load_workbook(tmp_path, read_only=True)
    ws = wb.active
    header = [str(c.value).strip().lower() if c.value else "" for c in next(ws.iter_rows(max_row=1))]
    required = {"question", "answer"}
    if not required.issubset(set(header)):
        raise HTTPException(422, "엑셀에 question, answer 컬럼이 필요합니다.")

    col_idx = {name: i for i, name in enumerate(header)}
    pool = await get_pool("voc_db_dsn")
    inserted = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        question = row[col_idx["question"]]
        answer = row[col_idx["answer"]]
        if not question or not answer:
            continue
        department = row[col_idx["department"]] if "department" in col_idx else None
        resolved_val = row[col_idx["resolved"]] if "resolved" in col_idx else True
        resolved = (
            str(resolved_val).strip().upper() not in ("FALSE", "0", "N", "NO")
            if resolved_val is not None
            else True
        )

        vec = await embed_text(f"{question}\n{answer}")
        await pool.execute(
            """
            INSERT INTO voc_records (question, answer, department, resolved, embedding)
            VALUES ($1, $2, $3, $4, $5::vector)
            """,
            str(question),
            str(answer),
            str(department) if department else None,
            resolved,
            vector_literal(vec),
        )
        inserted += 1

    return {"inserted": inserted}
