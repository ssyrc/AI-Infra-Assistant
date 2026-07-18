"""
Command MCP가 조회하는 커맨드 카탈로그(command_catalog) 관리 API.

개별 등록/수정/삭제와, 엑셀(열 매핑) 일괄 업로드를 지원한다.
등록/수정 시 name+description을 임베딩해 두어 Command MCP가 의미 검색을 할 수 있게 한다.
임베딩 서버 장애 시에도 등록은 막지 않는다(embedding=NULL로 저장, 키워드 검색은 계속 동작).
엑셀 미리보기/정제는 매뉴얼 탭과 같은 공통 모듈(uploads, spreadsheet, cleaning)을 쓴다.
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from auth import require_admin
from config_store import get_config
from db import get_pool, embed_text, vector_literal
from cleaning import clean_text, clean_options_from_dict
from spreadsheet import read_excel_meta, load_excel_rows
from uploads import (
    read_upload, create_upload_session, get_upload_session,
    delete_upload_session, load_options,
)

router = APIRouter(prefix="/api/commands", tags=["commands"])

_DSN = "command_db_dsn"


class CommandIn(BaseModel):
    name: str
    description: str
    usage: str | None = None
    category: str | None = None


async def _embed(text: str):
    """(vector_literal|None, model|None, dim|None). 임베딩 실패는 조용히 무시하고 NULL로 저장한다."""
    try:
        vec = await embed_text(text)
    except Exception as e:  # noqa: BLE001
        print(f"[commands] 임베딩 실패, embedding=NULL로 저장: {type(e).__name__}: {e}")
        return None, None, None
    model = await get_config("vllm_embed_model", "bge-m3")
    return vector_literal(vec), model, len(vec)


@router.get("")
async def list_commands(admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        "SELECT id, name, description, usage, category, updated_at, (embedding IS NOT NULL) AS embedded "
        "FROM command_catalog ORDER BY name"
    )
    return [dict(r) for r in rows]


@router.post("")
async def create_command(body: CommandIn, admin: str = Depends(require_admin)):
    emb, model, dim = await _embed(f"{body.name}\n{body.description}")
    pool = await get_pool(_DSN)
    try:
        row_id = await pool.fetchval(
            """
            INSERT INTO command_catalog (name, description, usage, category, embedding, embed_model, embed_dim)
            VALUES ($1, $2, $3, $4, $5::vector, $6, $7) RETURNING id
            """,
            body.name, body.description, body.usage, body.category, emb, model, dim,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"등록 실패 (이름 중복 가능): {e}")
    return {"id": row_id}


@router.patch("/{command_id}")
async def update_command(command_id: int, body: CommandIn, admin: str = Depends(require_admin)):
    emb, model, dim = await _embed(f"{body.name}\n{body.description}")
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        """
        UPDATE command_catalog
        SET name=$1, description=$2, usage=$3, category=$4,
            embedding=$5::vector, embed_model=$6, embed_dim=$7, updated_at=now()
        WHERE id=$8 RETURNING id
        """,
        body.name, body.description, body.usage, body.category, emb, model, dim, command_id,
    )
    if not row:
        raise HTTPException(404, "커맨드를 찾을 수 없습니다.")
    return {"ok": True}


@router.delete("/{command_id}")
async def delete_command(command_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    await pool.execute("DELETE FROM command_catalog WHERE id = $1", command_id)
    return {"ok": True}


# ---------------------------------------------------------------- 엑셀 일괄 업로드
@router.post("/excel/preview")
async def preview_command_excel(
    file: UploadFile = File(...),
    strip_html: bool = Form(True),
    collapse_space: bool = Form(True),
    drop_urls: bool = Form(False),
    admin: str = Depends(require_admin),
):
    """엑셀 열 목록과 샘플 행을 반환한다. 어떤 열을 name/description/usage/category로 쓸지 선택하게 한다."""
    ext, content = await read_upload(file, {".xlsx", ".xls"})
    options = {"strip_html": strip_html, "collapse_space": collapse_space, "drop_urls": drop_urls}
    upload_id = await create_upload_session(_DSN, admin, file.filename, ext, "command_catalog", content, options)
    session = await get_upload_session(_DSN, upload_id, admin, "command_catalog")

    try:
        sheet, header, sample, total = await run_in_threadpool(read_excel_meta, session["saved_path"])
    except Exception as e:  # noqa: BLE001
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, f"엑셀을 읽을 수 없습니다: {e}")

    if not header:
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, "빈 엑셀 파일입니다.")

    return {"upload_id": upload_id, "filename": file.filename, "sheet": sheet,
            "columns": header, "sample_rows": sample, "total_rows": total, "options": options}


class CommandExcelCommitIn(BaseModel):
    upload_id: str
    name_column: str
    description_column: str
    usage_column: str | None = None
    category_column: str | None = None


@router.post("/excel/commit")
async def commit_command_excel(body: CommandExcelCommitIn, admin: str = Depends(require_admin)):
    """선택한 열 매핑으로 커맨드를 일괄 등록/갱신한다(name 기준 upsert).
    같은 이름이 이미 있으면 내용을 갱신하고, 없으면 새로 추가한다."""
    session = await get_upload_session(_DSN, body.upload_id, admin, "command_catalog")
    opts = clean_options_from_dict(load_options(session))

    def _build(path: str):
        header, col_idx, rows = load_excel_rows(path)
        required = {"이름(name)": body.name_column, "설명(description)": body.description_column}
        for label, col in required.items():
            if col not in col_idx:
                raise ValueError(f"{label} 열이 엑셀에 없습니다: {col}")
        for col in (body.usage_column, body.category_column):
            if col and col not in col_idx:
                raise ValueError(f"존재하지 않는 열입니다: {col}")

        def _cell(row, col):
            if not col or col not in col_idx:
                return None
            val = row[col_idx[col]]
            return None if val is None else clean_text(str(val), opts)

        built = []
        for row in rows:
            name = _cell(row, body.name_column)
            desc = _cell(row, body.description_column)
            if not name or not desc:
                continue
            usage = _cell(row, body.usage_column) or None
            category = _cell(row, body.category_column) or None
            built.append((name.strip(), desc, usage, category))
        return built

    try:
        items = await run_in_threadpool(_build, session["saved_path"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        await delete_upload_session(_DSN, body.upload_id)

    if not items:
        raise HTTPException(422, "등록할 커맨드가 없습니다. 이름/설명 열 선택을 확인하세요.")

    pool = await get_pool(_DSN)
    inserted = updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for name, desc, usage, category in items:
                emb, model, dim = await _embed(f"{name}\n{desc}")
                res = await conn.fetchrow(
                    """
                    INSERT INTO command_catalog (name, description, usage, category, embedding, embed_model, embed_dim)
                    VALUES ($1, $2, $3, $4, $5::vector, $6, $7)
                    ON CONFLICT (name) DO UPDATE
                    SET description=EXCLUDED.description, usage=EXCLUDED.usage,
                        category=EXCLUDED.category, embedding=EXCLUDED.embedding,
                        embed_model=EXCLUDED.embed_model, embed_dim=EXCLUDED.embed_dim,
                        updated_at=now()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    name, desc, usage, category, emb, model, dim,
                )
                if res["inserted"]:
                    inserted += 1
                else:
                    updated += 1
    return {"inserted": inserted, "updated": updated, "total": len(items)}
