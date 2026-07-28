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
from spreadsheet import TABLE_EXTS, read_table_meta, load_table_rows
from uploads import (
    create_upload_session, get_upload_session,
    delete_upload_session, load_options,
)
from server_files import read_upload_or_server_file

router = APIRouter(prefix="/api/commands", tags=["commands"])

_DSN = "command_db_dsn"


class CommandIn(BaseModel):
    """카탈로그 항목은 이름/실행 커맨드/설명 3개로만 관리한다.
    exec_command: 실제 실행할 커맨드 문자열(비우면 name을 그대로 실행).
    Command MCP의 run_command가 셸 없이 shlex로 분해해 argv로 실행한다.
    `{user_id}` 토큰은 실행 시 호출자 본인 계정으로 치환된다(예: `phd info -u {user_id}`)."""
    name: str
    description: str
    exec_command: str | None = None


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
        "SELECT id, name, description, exec_command, updated_at, "
        "(embedding IS NOT NULL) AS embedded FROM command_catalog ORDER BY name"
    )
    return [dict(r) for r in rows]


@router.post("")
async def create_command(body: CommandIn, admin: str = Depends(require_admin)):
    emb, model, dim = await _embed(f"{body.name}\n{body.description}")
    pool = await get_pool(_DSN)
    try:
        row_id = await pool.fetchval(
            """
            INSERT INTO command_catalog (name, description, exec_command,
                                         embedding, embed_model, embed_dim)
            VALUES ($1, $2, $3, $4::vector, $5, $6) RETURNING id
            """,
            body.name, body.description, body.exec_command, emb, model, dim,
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
        SET name=$1, description=$2, exec_command=$3,
            embedding=$4::vector, embed_model=$5, embed_dim=$6, updated_at=now()
        WHERE id=$7 RETURNING id
        """,
        body.name, body.description, body.exec_command, emb, model, dim, command_id,
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
    file: UploadFile | None = File(None),
    server_path: str | None = Form(None),
    strip_html: bool = Form(True),
    collapse_space: bool = Form(True),
    drop_urls: bool = Form(False),
    admin: str = Depends(require_admin),
):
    """엑셀/CSV 열 목록과 샘플 행을 반환한다.
    어떤 열을 name/description/exec_command로 쓸지 선택하게 한다."""
    ext, content, filename = await read_upload_or_server_file(file, server_path, TABLE_EXTS)
    options = {"strip_html": strip_html, "collapse_space": collapse_space, "drop_urls": drop_urls}
    upload_id = await create_upload_session(_DSN, admin, filename, ext, "command_catalog", content, options)
    session = await get_upload_session(_DSN, upload_id, admin, "command_catalog")

    try:
        sheet, header, sample, total = await run_in_threadpool(read_table_meta, session["saved_path"])
    except Exception as e:  # noqa: BLE001
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, f"파일을 읽을 수 없습니다: {e}")

    if not header:
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, "빈 파일입니다(헤더 행이 없습니다).")

    return {"upload_id": upload_id, "filename": filename, "sheet": sheet,
            "columns": header, "sample_rows": sample, "total_rows": total, "options": options}


class CommandExcelCommitIn(BaseModel):
    """exec_command_column: 실제 실행할 커맨드가 적힌 열(선택). 지정하지 않으면 커맨드 이름을
    그대로 실행한다 - 이름 열에 실행 가능한 커맨드가 그대로 들어있는 카탈로그라면 매핑이 필요 없다."""
    upload_id: str
    name_column: str
    description_column: str
    exec_command_column: str | None = None


@router.post("/excel/commit")
async def commit_command_excel(body: CommandExcelCommitIn, admin: str = Depends(require_admin)):
    """선택한 열 매핑으로 커맨드를 일괄 등록/갱신한다(name 기준 upsert).
    같은 이름이 이미 있으면 내용을 갱신하고, 없으면 새로 추가한다."""
    session = await get_upload_session(_DSN, body.upload_id, admin, "command_catalog")
    opts = clean_options_from_dict(load_options(session))

    def _build(path: str):
        header, col_idx, rows = load_table_rows(path)
        required = {"이름(name)": body.name_column, "설명(description)": body.description_column}
        for label, col in required.items():
            if col not in col_idx:
                raise ValueError(f"{label} 열이 파일에 없습니다: {col}")
        for col in (body.exec_command_column,):
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
            exec_command = _cell(row, body.exec_command_column) or None
            built.append((name.strip(), desc, exec_command.strip() if exec_command else None))
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
            for name, desc, exec_command in items:
                emb, model, dim = await _embed(f"{name}\n{desc}")
                res = await conn.fetchrow(
                    """
                    INSERT INTO command_catalog (name, description, exec_command,
                                                 embedding, embed_model, embed_dim)
                    VALUES ($1, $2, $3, $4::vector, $5, $6)
                    ON CONFLICT (name) DO UPDATE
                    SET description=EXCLUDED.description,
                        -- 실행 커맨드 열을 매핑하지 않은 업로드가 기존 값을 지우지 않게 한다.
                        exec_command=COALESCE(EXCLUDED.exec_command, command_catalog.exec_command),
                        embedding=EXCLUDED.embedding,
                        embed_model=EXCLUDED.embed_model, embed_dim=EXCLUDED.embed_dim,
                        updated_at=now()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    name, desc, exec_command, emb, model, dim,
                )
                if res["inserted"]:
                    inserted += 1
                else:
                    updated += 1
    return {"inserted": inserted, "updated": updated, "total": len(items)}
