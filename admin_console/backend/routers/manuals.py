"""
매뉴얼(엑셀/워드/PPT) 업로드 -> 파싱 미리보기 -> 편집 -> 발행 -> 버전/롤백 API.

버전 모델: 같은 title로 재업로드하면 새 manual_files 행(version+1, status=draft)이 생긴다.
'발행(publish)'하면 해당 행의 미임베딩 청크만 임베딩하고 status=published로 바꾸며,
같은 title의 기존 published 행은 archived로 내려간다.
'롤백'은 과거 archived 버전을 다시 publish 호출하는 것과 동일하다(이미 임베딩되어 있어 즉시 반영).
"""
import os
import uuid
import tempfile

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from auth import require_admin
from db import get_pool, embed_text, vector_literal
from parser import parse_file

router = APIRouter(prefix="/api/manuals", tags=["manuals"])


@router.get("")
async def list_manuals(admin: str = Depends(require_admin)):
    pool = await get_pool("manual_db_dsn")
    rows = await pool.fetch(
        """
        SELECT id, title, filename, version, status, uploaded_by, uploaded_at, published_at
        FROM manual_files
        ORDER BY title, version DESC
        """
    )
    return [dict(r) for r in rows]


@router.get("/{manual_id}")
async def get_manual(manual_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("manual_db_dsn")
    file_row = await pool.fetchrow("SELECT * FROM manual_files WHERE id = $1", manual_id)
    if not file_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    chunks = await pool.fetch(
        "SELECT id, seq, section_title, page_no, chunk_text, (embedding IS NOT NULL) AS embedded "
        "FROM manual_chunks WHERE manual_file_id = $1 ORDER BY seq",
        manual_id,
    )
    return {"file": dict(file_row), "chunks": [dict(c) for c in chunks]}


@router.get("/{manual_id}/versions")
async def list_versions(manual_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("manual_db_dsn")
    title_row = await pool.fetchrow("SELECT title FROM manual_files WHERE id = $1", manual_id)
    if not title_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    rows = await pool.fetch(
        "SELECT id, version, status, uploaded_at, published_at FROM manual_files "
        "WHERE title = $1 ORDER BY version DESC",
        title_row["title"],
    )
    return [dict(r) for r in rows]


@router.post("/upload")
async def upload_manual(
    file: UploadFile = File(...),
    title: str = Form(...),
    uploaded_by: str = Depends(require_admin),
):
    suffix = os.path.splitext(file.filename)[1].lower()
    if suffix in (".xlsx", ".xls"):
        raise HTTPException(
            422,
            "엑셀 파일은 /api/manuals/excel/preview 흐름을 사용하세요 "
            "(컬럼을 선택해서 등록해야 합니다).",
        )
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        chunks = parse_file(tmp_path)
    finally:
        os.unlink(tmp_path)

    if not chunks:
        raise HTTPException(422, "문서에서 추출된 내용이 없습니다.")

    pool = await get_pool("manual_db_dsn")
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM manual_files WHERE title = $1", title
            )
            file_id = await conn.fetchval(
                """
                INSERT INTO manual_files (title, filename, source_type, uploaded_by, version, status)
                VALUES ($1, $2, 'document', $3, $4, 'draft')
                RETURNING id
                """,
                title,
                file.filename,
                uploaded_by,
                next_version,
            )
            for seq, c in enumerate(chunks):
                await conn.execute(
                    """
                    INSERT INTO manual_chunks (manual_file_id, seq, section_title, page_no, chunk_text)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    file_id,
                    seq,
                    c.section_title,
                    c.page_no,
                    c.chunk_text,
                )
    return {"manual_file_id": file_id, "version": next_version, "chunk_count": len(chunks)}


# --- 엑셀 전용 흐름: 업로드 -> 컬럼 미리보기 -> 컬럼 선택 후 커밋 ------------------
_EXCEL_TMP_DIR = tempfile.gettempdir()


@router.post("/excel/preview")
async def preview_excel(file: UploadFile = File(...), admin: str = Depends(require_admin)):
    """엑셀을 업로드하면 시트의 컬럼 목록과 샘플 행(최대 5개)을 반환한다.
    실제 등록은 /excel/commit에서 컬럼을 선택한 뒤 진행한다."""
    import openpyxl

    upload_id = f"manual-xlsx-{uuid.uuid4().hex[:12]}"
    tmp_path = os.path.join(_EXCEL_TMP_DIR, f"{upload_id}.xlsx")
    with open(tmp_path, "wb") as f:
        f.write(await file.read())

    wb = openpyxl.load_workbook(tmp_path, read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(v).strip() if v is not None else f"column_{i}" for i, v in enumerate(next(rows_iter))]
    sample = []
    for i, row in enumerate(rows_iter):
        if i >= 5:
            break
        sample.append(list(row))

    return {"upload_id": upload_id, "filename": file.filename, "columns": header, "sample_rows": sample}


class ExcelCommitIn(BaseModel):
    upload_id: str
    title: str
    content_columns: list[str]
    title_column: str | None = None


@router.post("/excel/commit")
async def commit_excel(body: ExcelCommitIn, uploaded_by: str = Depends(require_admin)):
    """preview에서 선택한 컬럼들로 행 단위 청크를 만들어 draft 문서로 등록한다.
    content_columns에 담긴 값들을 'col: value' 형태로 이어붙여 chunk_text를 구성하고,
    title_column을 지정하면 그 값이 section_title로 들어가 검색 결과 표시에 쓰인다."""
    import openpyxl

    tmp_path = os.path.join(_EXCEL_TMP_DIR, f"{body.upload_id}.xlsx")
    if not os.path.exists(tmp_path):
        raise HTTPException(404, "업로드 세션이 만료되었습니다. 다시 업로드하세요.")
    if not body.content_columns:
        raise HTTPException(422, "내용으로 사용할 컬럼을 1개 이상 선택하세요.")

    wb = openpyxl.load_workbook(tmp_path, read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(v).strip() if v is not None else f"column_{i}" for i, v in enumerate(next(rows_iter))]
    col_idx = {name: i for i, name in enumerate(header)}
    for c in body.content_columns:
        if c not in col_idx:
            raise HTTPException(422, f"존재하지 않는 컬럼입니다: {c}")

    chunks = []
    for row in rows_iter:
        if all(v is None for v in row):
            continue
        content = "\n".join(
            f"{c}: {row[col_idx[c]]}" for c in body.content_columns if row[col_idx[c]] is not None
        )
        if not content:
            continue
        section_title = None
        if body.title_column and body.title_column in col_idx:
            section_title = row[col_idx[body.title_column]]
            section_title = str(section_title) if section_title is not None else None
        chunks.append((section_title, content))
    os.unlink(tmp_path)

    if not chunks:
        raise HTTPException(422, "선택한 컬럼으로 만들어진 내용이 없습니다.")

    pool = await get_pool("manual_db_dsn")
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM manual_files WHERE title = $1", body.title
            )
            file_id = await conn.fetchval(
                """
                INSERT INTO manual_files (title, filename, source_type, uploaded_by, version, status)
                VALUES ($1, $2, 'spreadsheet', $3, $4, 'draft')
                RETURNING id
                """,
                body.title,
                f"{body.title}.xlsx",
                uploaded_by,
                next_version,
            )
            for seq, (section_title, content) in enumerate(chunks):
                await conn.execute(
                    """
                    INSERT INTO manual_chunks (manual_file_id, seq, section_title, chunk_text)
                    VALUES ($1, $2, $3, $4)
                    """,
                    file_id,
                    seq,
                    section_title,
                    content,
                )
    return {"manual_file_id": file_id, "version": next_version, "chunk_count": len(chunks)}


@router.patch("/chunks/{chunk_id}")
async def update_chunk(chunk_id: int, chunk_text: str = Form(...), admin: str = Depends(require_admin)):
    """운영자가 자동 추출된 청크를 직접 교정한다. 발행 전 draft 상태에서만 의미가 있다.
    이미 임베딩된(published 되었던) 청크를 수정하면 재발행 시 다시 임베딩되도록 embedding을 초기화한다."""
    pool = await get_pool("manual_db_dsn")
    row = await pool.fetchrow(
        "UPDATE manual_chunks SET chunk_text = $1, embedding = NULL WHERE id = $2 RETURNING id",
        chunk_text,
        chunk_id,
    )
    if not row:
        raise HTTPException(404, "청크를 찾을 수 없습니다.")
    return {"ok": True}


@router.delete("/chunks/{chunk_id}")
async def delete_chunk(chunk_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("manual_db_dsn")
    await pool.execute("DELETE FROM manual_chunks WHERE id = $1", chunk_id)
    return {"ok": True}


@router.post("/{manual_id}/publish")
async def publish_manual(manual_id: int, admin: str = Depends(require_admin)):
    """draft/archived 버전을 발행(=검색 대상으로 전환)한다.
    archived 버전에 대해 호출하면 사실상 '롤백'과 동일하게 동작한다."""
    pool = await get_pool("manual_db_dsn")
    file_row = await pool.fetchrow("SELECT * FROM manual_files WHERE id = $1", manual_id)
    if not file_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")

    unembedded = await pool.fetch(
        "SELECT id, chunk_text FROM manual_chunks WHERE manual_file_id = $1 AND embedding IS NULL",
        manual_id,
    )
    for c in unembedded:
        vec = await embed_text(c["chunk_text"])
        await pool.execute(
            "UPDATE manual_chunks SET embedding = $1::vector WHERE id = $2",
            vector_literal(vec),
            c["id"],
        )

    async with (await get_pool("manual_db_dsn")).acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE manual_files SET status = 'archived' "
                "WHERE title = $1 AND status = 'published' AND id != $2",
                file_row["title"],
                manual_id,
            )
            await conn.execute(
                "UPDATE manual_files SET status = 'published', published_at = now() WHERE id = $1",
                manual_id,
            )
    return {"ok": True, "embedded_chunks": len(unembedded)}


@router.delete("/{manual_id}")
async def delete_manual(manual_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool("manual_db_dsn")
    status_row = await pool.fetchrow("SELECT status FROM manual_files WHERE id = $1", manual_id)
    if not status_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if status_row["status"] == "published":
        raise HTTPException(400, "발행 중인 버전은 삭제할 수 없습니다. 먼저 다른 버전을 발행하세요.")
    await pool.execute("DELETE FROM manual_files WHERE id = $1", manual_id)
    return {"ok": True}
