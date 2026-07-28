"""
매뉴얼(엑셀/워드/PPT) 업로드 -> 파싱 미리보기 -> 편집 -> 발행 -> 버전/롤백 API.

버전 모델: 같은 title로 재업로드하면 새 manual_files 행(version+1, status=draft)이 생긴다.
'발행(publish)'하면 해당 행의 미임베딩 청크만 임베딩하고 status=published로 바꾸며,
같은 title의 기존 published 행은 archived로 내려간다.
'롤백'은 과거 archived 버전을 다시 publish 호출하는 것과 동일하다(이미 임베딩되어 있어 즉시 반영).
'발행취소(unpublish)'는 published 버전을 draft로 내려 즉시 검색에서 제외한다.

업로드 세션/엑셀 읽기/정제는 공통 모듈(uploads, spreadsheet, cleaning)을 공유한다.
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from auth import require_admin
from config_store import get_config
from db import get_pool, embed_text, vector_literal, get_http_client
from parser import parse_file, SUPPORTED_EXTS
from cleaning import clean_text, clean_options_from_dict, CleanOptions
from spreadsheet import TABLE_EXTS, read_table_meta, load_table_rows
from uploads import (
    create_upload_session, get_upload_session,
    delete_upload_session, load_options,
)
from server_files import read_upload_or_server_file

router = APIRouter(prefix="/api/manuals", tags=["manuals"])

_DSN = "manual_db_dsn"


@router.get("")
async def list_manuals(admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        """
        SELECT id, title, filename, source_type, version, status, uploaded_by, uploaded_at, published_at
        FROM manual_files
        ORDER BY title, version DESC
        """
    )
    return [dict(r) for r in rows]


@router.get("/{manual_id}")
async def get_manual(manual_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
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
    pool = await get_pool(_DSN)
    title_row = await pool.fetchrow("SELECT title FROM manual_files WHERE id = $1", manual_id)
    if not title_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    rows = await pool.fetch(
        "SELECT id, version, status, uploaded_at, published_at FROM manual_files "
        "WHERE title = $1 ORDER BY version DESC",
        title_row["title"],
    )
    return [dict(r) for r in rows]


async def _insert_draft(title: str, filename: str, source_type: str, uploaded_by: str,
                        chunks: list) -> dict:
    """청크 리스트를 새 draft 버전으로 저장하는 공통 헬퍼. chunks는 (section_title, page_no, text) 튜플."""
    pool = await get_pool(_DSN)
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM manual_files WHERE title = $1", title
            )
            file_id = await conn.fetchval(
                """
                INSERT INTO manual_files (title, filename, source_type, uploaded_by, version, status)
                VALUES ($1, $2, $3, $4, $5, 'draft')
                RETURNING id
                """,
                title, filename, source_type, uploaded_by, next_version,
            )
            for seq, (section_title, page_no, text) in enumerate(chunks):
                await conn.execute(
                    """
                    INSERT INTO manual_chunks (manual_file_id, seq, section_title, page_no, chunk_text)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    file_id, seq, section_title, page_no, text,
                )
    return {"manual_file_id": file_id, "version": next_version, "chunk_count": len(chunks)}


@router.post("/preview")
async def preview_document(
    file: UploadFile | None = File(None),
    server_path: str | None = Form(None),
    strip_html: bool = Form(True),
    collapse_space: bool = Form(True),
    drop_urls: bool = Form(False),
    include_speaker_notes: bool = Form(False),
    admin: str = Depends(require_admin),
):
    """docx/pptx/pdf/txt/md 전처리 미리보기. 아직 DB에 저장하지 않는다.
    정제 옵션은 서버가 세션에 저장하고, commit 때 그 옵션을 그대로 사용한다.
    file(브라우저 업로드) 또는 server_path(서버에 마운트된 폴더에서 선택) 중 하나를 받는다."""
    ext, content, filename = await read_upload_or_server_file(file, server_path, SUPPORTED_EXTS)
    options = {
        "strip_html": strip_html, "collapse_space": collapse_space,
        "drop_urls": drop_urls, "include_speaker_notes": include_speaker_notes,
    }
    upload_id = await create_upload_session(_DSN, admin, filename, ext, "document", content, options)
    session = await get_upload_session(_DSN, upload_id, admin, "document")

    opts = CleanOptions(strip_html=strip_html, collapse_space=collapse_space, drop_urls=drop_urls)
    try:
        chunks = await run_in_threadpool(
            parse_file, session["saved_path"], opts, include_speaker_notes)
    except Exception as e:  # noqa: BLE001
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, f"전처리 실패: {e}")

    if not chunks:
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, "문서에서 추출된 내용이 없습니다.")

    preview = [
        {"seq": i, "section_title": c.section_title, "page_no": c.page_no,
         "chunk_text": c.chunk_text, "char_count": len(c.chunk_text)}
        for i, c in enumerate(chunks[:50])
    ]
    return {"upload_id": upload_id, "filename": filename,
            "total_chunks": len(chunks), "preview_chunks": preview, "options": options}


class DocCommitIn(BaseModel):
    upload_id: str
    title: str = Field(min_length=1, max_length=200)


@router.post("/commit")
async def commit_document(body: DocCommitIn, uploaded_by: str = Depends(require_admin)):
    """preview에서 확인한 문서를 draft로 저장한다.
    확장자·경로·정제 옵션은 모두 서버 세션에서 가져오므로 미리보기 결과와 반드시 일치한다."""
    session = await get_upload_session(_DSN, body.upload_id, uploaded_by, "document")
    saved_options = load_options(session)
    opts = clean_options_from_dict(saved_options)

    try:
        parsed = await run_in_threadpool(
            parse_file, session["saved_path"], opts,
            bool(saved_options.get("include_speaker_notes", False)))
    finally:
        await delete_upload_session(_DSN, body.upload_id)

    if not parsed:
        raise HTTPException(422, "문서에서 추출된 내용이 없습니다.")
    chunks = [(c.section_title, c.page_no, c.chunk_text) for c in parsed]
    return await _insert_draft(body.title, session["filename"], "document", uploaded_by, chunks)


@router.post("/excel/preview")
async def preview_excel(
    file: UploadFile | None = File(None),
    server_path: str | None = Form(None),
    strip_html: bool = Form(True),
    collapse_space: bool = Form(True),
    drop_urls: bool = Form(False),
    admin: str = Depends(require_admin),
):
    """엑셀/CSV 컬럼 목록과 샘플 행, 전체 행 수를 반환한다."""
    ext, content, filename = await read_upload_or_server_file(file, server_path, TABLE_EXTS)
    options = {"strip_html": strip_html, "collapse_space": collapse_space, "drop_urls": drop_urls}
    upload_id = await create_upload_session(_DSN, admin, filename, ext, "spreadsheet", content, options)
    session = await get_upload_session(_DSN, upload_id, admin, "spreadsheet")

    try:
        # 헤더 행은 자동 판별한다(위에 제목 줄이 있어도 됨). commit도 같은 파일을 같은 방식으로
        # 읽으므로 결과가 어긋나지 않는다.
        sheet, header, sample, total, header_row = await run_in_threadpool(
            read_table_meta, session["saved_path"])
    except Exception as e:  # noqa: BLE001
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, f"파일을 읽을 수 없습니다: {e}")

    if not header:
        await delete_upload_session(_DSN, upload_id)
        raise HTTPException(422, "빈 파일입니다(헤더 행이 없습니다).")

    return {"upload_id": upload_id, "filename": filename, "sheet": sheet,
            "columns": header, "sample_rows": sample, "total_rows": total,
            "header_row": header_row, "options": options}


class ExcelCommitIn(BaseModel):
    upload_id: str
    title: str = Field(min_length=1, max_length=200)
    content_columns: list[str] = Field(min_length=1)
    title_column: str | None = None
    page_no_column: str | None = None


@router.post("/excel/commit")
async def commit_excel(body: ExcelCommitIn, uploaded_by: str = Depends(require_admin)):
    """선택한 컬럼들로 행 단위 청크를 만들어 draft로 등록한다.
    정제 옵션은 preview 당시 서버에 저장된 값을 사용한다."""
    session = await get_upload_session(_DSN, body.upload_id, uploaded_by, "spreadsheet")
    opts = clean_options_from_dict(load_options(session))

    def _build(path: str):
        header, col_idx, rows = load_table_rows(path)
        for c in body.content_columns:
            if c not in col_idx:
                raise ValueError(f"존재하지 않는 컬럼입니다: {c}")

        built = []
        for row in rows:
            parts = []
            for c in body.content_columns:
                val = row[col_idx[c]]
                if val is None:
                    continue
                cleaned = clean_text(str(val), opts)
                if cleaned:
                    parts.append(f"{c}: {cleaned}")
            content = "\n".join(parts)
            if not content:
                continue
            section_title = None
            if body.title_column and body.title_column in col_idx:
                tv = row[col_idx[body.title_column]]
                section_title = clean_text(str(tv), opts) if tv is not None else None
            page_no = None
            if body.page_no_column and body.page_no_column in col_idx:
                pv = row[col_idx[body.page_no_column]]
                try:
                    page_no = int(pv) if pv is not None else None
                except (TypeError, ValueError):
                    page_no = None
            built.append((section_title or None, page_no, content))
        return built

    try:
        chunks = await run_in_threadpool(_build, session["saved_path"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        await delete_upload_session(_DSN, body.upload_id)

    if not chunks:
        raise HTTPException(422, "선택한 컬럼으로 만들어진 내용이 없습니다.")
    return await _insert_draft(body.title, session["filename"], "spreadsheet", uploaded_by, chunks)


async def _assert_chunk_editable(pool, chunk_id: int) -> None:
    """청크의 부모 문서가 draft일 때만 편집을 허용한다.
    published/archived 문서를 수정하면 이미 서비스 중인 내용이 조용히 바뀌거나
    임베딩과 텍스트가 어긋나므로 금지한다."""
    row = await pool.fetchrow(
        """
        SELECT f.status FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.id = $1
        """,
        chunk_id,
    )
    if not row:
        raise HTTPException(404, "청크를 찾을 수 없습니다.")
    if row["status"] != "draft":
        raise HTTPException(
            409,
            f"'{row['status']}' 상태의 문서는 수정할 수 없습니다. "
            "발행취소하거나 같은 제목으로 새 버전을 업로드해 수정하세요.",
        )


@router.patch("/chunks/{chunk_id}")
async def update_chunk(chunk_id: int, chunk_text: str = Form(...), admin: str = Depends(require_admin)):
    """운영자가 자동 추출된 청크를 직접 교정한다. draft 상태에서만 허용된다."""
    pool = await get_pool(_DSN)
    await _assert_chunk_editable(pool, chunk_id)
    await pool.execute(
        "UPDATE manual_chunks SET chunk_text = $1, embedding = NULL WHERE id = $2",
        chunk_text, chunk_id,
    )
    return {"ok": True}


@router.delete("/chunks/{chunk_id}")
async def delete_chunk(chunk_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    await _assert_chunk_editable(pool, chunk_id)
    await pool.execute("DELETE FROM manual_chunks WHERE id = $1", chunk_id)
    return {"ok": True}


@router.post("/{manual_id}/publish")
async def publish_manual(manual_id: int, admin: str = Depends(require_admin)):
    """draft/archived 버전을 발행(=검색 대상으로 전환)한다.
    archived 버전에 대해 호출하면 '롤백'과 동일하게 동작한다.

    동시성: 같은 title에 대해 두 사람이 동시에 발행하면 published가 2개가 될 수 있으므로
    title 기준 advisory lock으로 직렬화한다.
    임베딩이 하나라도 실패하면 상태를 바꾸지 않는다(부분 발행 방지)."""
    pool = await get_pool(_DSN)
    file_row = await pool.fetchrow("SELECT * FROM manual_files WHERE id = $1", manual_id)
    if not file_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if file_row["status"] == "published":
        return {"ok": True, "embedded_chunks": 0, "message": "이미 발행된 버전입니다."}

    embed_model = await get_config("vllm_embed_model", "bge-m3")
    try:
        embed_dim = int(await get_config("embed_dim", "1024"))
    except (TypeError, ValueError):
        embed_dim = 1024

    unembedded = await pool.fetch(
        "SELECT id, chunk_text FROM manual_chunks WHERE manual_file_id = $1 AND embedding IS NULL",
        manual_id,
    )
    embedded_count = 0
    for c in unembedded:
        try:
            vec = await embed_text(c["chunk_text"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                503,
                f"임베딩 서버 오류로 발행을 중단했습니다({embedded_count}/{len(unembedded)}개 완료). "
                f"서버 상태를 확인한 뒤 다시 발행하세요. 원인: {e}",
            )
        if len(vec) != embed_dim:
            raise HTTPException(
                500,
                f"임베딩 차원이 맞지 않습니다(모델 {len(vec)} vs 스키마 {embed_dim}). "
                "설정의 embed_dim과 임베딩 모델을 확인하세요.",
            )
        await pool.execute(
            "UPDATE manual_chunks SET embedding = $1::vector, embed_model = $2, embed_dim = $3 WHERE id = $4",
            vector_literal(vec), embed_model, embed_dim, c["id"],
        )
        embedded_count += 1

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 같은 title 발행을 직렬화 (advisory lock, 트랜잭션 종료 시 자동 해제)
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", file_row["title"])
            await conn.execute(
                "UPDATE manual_files SET status = 'archived' "
                "WHERE title = $1 AND status = 'published' AND id != $2",
                file_row["title"], manual_id,
            )
            await conn.execute(
                "UPDATE manual_files SET status = 'published', published_at = now() WHERE id = $1",
                manual_id,
            )
    return {"ok": True, "embedded_chunks": embedded_count}


@router.post("/{manual_id}/unpublish")
async def unpublish_manual(manual_id: int, admin: str = Depends(require_admin)):
    """발행 중인 버전을 즉시 검색 대상에서 제외한다(draft로 내림).
    manual MCP는 status='published'인 문서만 검색하므로, 이 호출 즉시 반영된다.
    이후 이 버전은 청크를 교정하거나 삭제할 수 있고, 다시 발행하면 즉시 복귀한다
    (임베딩은 유지되므로 재임베딩 없이 빠르게 재발행된다)."""
    pool = await get_pool(_DSN)
    row = await pool.fetchrow("SELECT status FROM manual_files WHERE id = $1", manual_id)
    if not row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if row["status"] != "published":
        raise HTTPException(400, "발행 중인 버전만 발행취소할 수 있습니다.")
    await pool.execute(
        "UPDATE manual_files SET status = 'draft', published_at = NULL WHERE id = $1",
        manual_id,
    )
    return {"ok": True}


@router.delete("/{manual_id}")
async def delete_manual(manual_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    status_row = await pool.fetchrow("SELECT status FROM manual_files WHERE id = $1", manual_id)
    if not status_row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if status_row["status"] == "published":
        raise HTTPException(400, "발행 중인 버전은 삭제할 수 없습니다. 먼저 발행취소하세요.")
    await pool.execute("DELETE FROM manual_files WHERE id = $1", manual_id)
    return {"ok": True}


# ---------------------------------------------------------------- 임베딩 상태/재생성
# 임베딩은 "등록 시점의 임베딩 서버 설정"으로 만들어진다. 설정이 잘못돼 있던 동안(예: mock
# 주소로 되돌아간 상태) 올린 문서는 임베딩이 없거나(NULL) 다른 모델의 벡터를 갖게 되고,
# 그러면 의미 검색이 엉뚱한 청크를 물어온다("GPU 사용법"에 CPU 문서가 나오는 증상).
# 여기서 현재 설정과 어긋난 청크를 찾아 다시 임베딩할 수 있게 한다.
async def _embed_settings() -> tuple[str, int]:
    model = await get_config("vllm_embed_model", "bge-m3")
    try:
        dim = int(await get_config("embed_dim", "1024"))
    except (TypeError, ValueError):
        dim = 1024
    return model, dim


def _stale_where() -> str:
    """현재 설정과 어긋나 다시 임베딩해야 하는 청크 조건(발행된 문서만 검색에 쓰이므로 전체 대상)."""
    return ("embedding IS NULL OR embed_model IS DISTINCT FROM $1 "
            "OR embed_dim IS DISTINCT FROM $2")


@router.get("/embedding-status")
async def embedding_status(admin: str = Depends(require_admin)):
    """현재 임베딩 설정과 저장된 청크 임베딩이 맞는지 알려준다."""
    model, dim = await _embed_settings()
    pool = await get_pool(_DSN)
    total = await pool.fetchval("SELECT count(*) FROM manual_chunks")
    stale = await pool.fetchval(
        f"SELECT count(*) FROM manual_chunks WHERE {_stale_where()}", model, dim)
    by_model = await pool.fetch(
        "SELECT embed_model, embed_dim, count(*) AS count FROM manual_chunks "
        "GROUP BY embed_model, embed_dim ORDER BY count DESC"
    )
    return {"current_model": model, "current_dim": dim, "total_chunks": total,
            "stale_chunks": stale, "by_model": [dict(r) for r in by_model]}


@router.post("/reembed")
async def reembed(limit: int = 300, admin: str = Depends(require_admin)):
    """현재 설정과 어긋난 청크를 다시 임베딩한다(한 번에 limit개, 남으면 이어서 호출).
    임베딩 서버가 죽어 있으면 즉시 중단하고 몇 개까지 했는지 알려준다."""
    model, dim = await _embed_settings()
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        f"SELECT id, chunk_text FROM manual_chunks WHERE {_stale_where()} ORDER BY id LIMIT $3",
        model, dim, max(1, min(int(limit), 1000)),
    )
    done = 0
    for c in rows:
        try:
            vec = await embed_text(c["chunk_text"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"임베딩 서버 오류로 중단했습니다({done}개 완료). 원인: {e}")
        if len(vec) != dim:
            raise HTTPException(
                500, f"임베딩 차원이 맞지 않습니다(모델 {len(vec)} vs 스키마 {dim}). "
                     "설정의 임베딩 모델/embed_dim을 확인하세요.")
        await pool.execute(
            "UPDATE manual_chunks SET embedding = $1::vector, embed_model = $2, embed_dim = $3 "
            "WHERE id = $4", vector_literal(vec), model, dim, c["id"])
        done += 1
    remaining = await pool.fetchval(
        f"SELECT count(*) FROM manual_chunks WHERE {_stale_where()}", model, dim)
    return {"processed": done, "remaining": remaining}


# ---------------------------------------------------------------- 검색 진단
# "왜 GPU를 물었는데 CPU 문서가 나오나"를 눈으로 확인하기 위한 도구.
# Manual MCP의 search_manual과 '완전히 같은' 쿼리를 돌리되, 조용히 fallback되는 지점
# (임베딩 실패 -> 키워드 전용, 리랭커 실패 -> RRF 순위)을 숨기지 않고 그대로 보여준다.
async def _rerank_probe(query: str, docs: list[str]) -> dict:
    """리랭커를 직접 호출해 성공/실패와 원인을 그대로 돌려준다(rerank()는 실패를 삼킨다)."""
    base_url = await get_config("rerank_base_url", "")
    provider = (await get_config("rerank_provider", "tei") or "tei").lower()
    if not base_url or provider == "none":
        return {"used": False, "reason": "rerank_base_url이 비었거나 provider=none"}
    model = await get_config("rerank_model", "bge-reranker-v2-m3")
    url = f"{base_url.rstrip('/')}/rerank"
    if provider == "vllm":
        payload = {"model": model, "query": query, "documents": docs}
    else:
        payload = {"query": query, "texts": docs, "raw_scores": False}
    try:
        client = await get_http_client()
        resp = await client.post(url, json=payload, timeout=10.0)
        if resp.status_code >= 400:
            return {"used": False, "provider": provider, "url": url,
                    "reason": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    "hint": "provider 설정이 서버 종류와 다를 수 있습니다(vllm ↔ tei)."}
        return {"used": True, "provider": provider, "url": url}
    except Exception as e:  # noqa: BLE001
        return {"used": False, "provider": provider, "url": url,
                "reason": f"{type(e).__name__}: {e}"}


class SearchTestIn(BaseModel):
    query: str
    top_k: int = 10


@router.post("/search-test")
async def search_test(body: SearchTestIn, admin: str = Depends(require_admin)):
    """관리자 콘솔에서 매뉴얼 검색을 그대로 재현하고, 어느 단계가 죽었는지 보여준다."""
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(422, "검색어를 입력하세요.")
    top_k = max(1, min(int(body.top_k), 20))
    pool = await get_pool(_DSN)

    embed_info: dict = {}
    vec = None
    try:
        vec = await embed_text(query)
        embed_info = {"ok": True, "dim": len(vec)}
    except Exception as e:  # noqa: BLE001
        embed_info = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    published = await pool.fetchval(
        "SELECT count(*) FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id "
        "WHERE f.status = 'published'")
    with_vec = await pool.fetchval(
        "SELECT count(*) FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id "
        "WHERE f.status = 'published' AND c.embedding IS NOT NULL")

    if vec is None:
        mode = "키워드 전용(임베딩 실패)"
        rows = await pool.fetch(
            """
            SELECT c.id, c.section_title, c.chunk_text, f.title,
                   ts_rank(c.tsv, plainto_tsquery('simple', $1)) AS score
            FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.tsv @@ plainto_tsquery('simple', $1)
            ORDER BY score DESC LIMIT $2
            """, query, top_k)
    else:
        mode = "하이브리드(의미+키워드)"
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(c.tsv, plainto_tsquery('simple', $2)) DESC) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.tsv @@ plainto_tsquery('simple', $2) LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id, v.rank AS vrank, k.rank AS krank,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0) AS rrf
                FROM vector_search v FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT c.id, c.section_title, c.chunk_text, f.title,
                   fused.rrf AS score, fused.vrank, fused.krank
            FROM fused JOIN manual_chunks c ON c.id = fused.id
            JOIN manual_files f ON f.id = c.manual_file_id
            ORDER BY fused.rrf DESC LIMIT $3
            """, vector_literal(vec), query, top_k)

    hits = [dict(r) for r in rows]
    docs = [(f"{h['section_title']}\n{h['chunk_text']}" if h.get("section_title")
             else h["chunk_text"]) for h in hits]
    rerank_info = await _rerank_probe(query, docs) if docs else {"used": False, "reason": "후보 없음"}

    for h in hits:
        h["chunk_text"] = (h["chunk_text"] or "")[:300]
        h["score"] = float(h["score"]) if h["score"] is not None else None
    try:
        min_score = float(await get_config("rerank_min_score", "0.05"))
    except (TypeError, ValueError):
        min_score = 0.05
    return {"mode": mode, "embedding": embed_info, "rerank": rerank_info,
            "min_score": min_score,
            "published_chunks": published, "chunks_with_embedding": with_vec, "hits": hits}
