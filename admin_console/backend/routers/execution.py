"""
Execution MCP 관리 API - 등록 커맨드(execution_commands) + 실행 감사로그.
구 commands.py(커맨드 카탈로그)와 system.py(화이트리스트)를 하나로 합친 것이다(#111).

등록 커맨드의 인자 설계:
  관리자는 `head -n {lines} {path}`처럼 **자리표시자가 든 커맨드 한 줄**만 적는다. 콘솔이
  자리표시자를 뽑아 인자 표를 만들고, 각 인자의 타입/필수/기본값/설명을 채우게 한다.
  argv JSON을 손으로 쓰게 하던 예전 방식보다 훨씬 쉽고, 카탈로그의 `{user_id}` 문법과도 같다.
  `{user_id}`는 예약어라 표에 나오지 않는다(호출자 신원에서 자동 주입).

**커맨드는 전부 여기 등록분 하나다**(#128). 예전에는 파이썬 함수로 박아 둔 '내장 커맨드' 7개가
따로 있어서 편집도 삭제도 안 됐는데, 그 7개는 전부 LLM이 이미 아는 표준 리눅스 명령이라
run_command로 실행하면 그만이다. 목록에서 없앴더니 매 요청 프롬프트도 그만큼 가벼워졌다.
"""
import json
import sys
import os

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from auth import require_admin
from config_store import get_config
from db import get_pool
from cleaning import clean_text, clean_options_from_dict
from spreadsheet import TABLE_EXTS, read_table_meta, load_table_rows
from uploads import (
    create_upload_session, get_upload_session, delete_upload_session, load_options,
)
from server_files import read_upload_or_server_file

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../shared"))
from execution_exec import (  # noqa: E402
    DEFAULT_DENY_CSV, deny_set, placeholders_in, tool_name_for, validate_definition,
)

router = APIRouter(prefix="/api/execution", tags=["execution"])

_DSN = "execution_db_dsn"
# 툴 이름이 겹치면 안 되는 예약어. run_command는 MCP가 항상 노출하는 미등록 커맨드 실행 툴이다.
_RESERVED_TOOL_NAMES = {"run_command"}


async def _deny() -> set:
    return deny_set(await get_config("execution_deny_commands", DEFAULT_DENY_CSV))


def _row(r) -> dict:
    d = dict(r)
    if isinstance(d.get("args"), str):
        d["args"] = json.loads(d["args"])
    return d


# ---------------------------------------------------------------- 등록 커맨드
class ArgIn(BaseModel):
    name: str
    type: str = "str"                 # str | int | enum
    required: bool = False
    default: str = ""
    description: str = ""
    choices: list[str] = []


class CommandIn(BaseModel):
    """title: 사람이 읽는 이름(한글 가능). tool_name은 서버가 만들어 준다(ASCII 규칙).
    exec_command: `myquota` / `head -n {lines} {path}` 형태의 커맨드 한 줄."""
    title: str
    description: str = ""
    exec_command: str
    args: list[ArgIn] = []
    host_mode: str = "login_server"
    enabled: bool = True
    required_roles: list[str] = []


@router.get("/commands")
async def list_commands(admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        "SELECT id, tool_name, title, description, exec_command, args, "
        "host_mode, enabled, required_roles, updated_by, updated_at "
        "FROM execution_commands ORDER BY title")
    return [_row(r) for r in rows]


@router.post("/commands/parse")
async def parse_command(body: dict, admin: str = Depends(require_admin)):
    """커맨드 한 줄에서 자리표시자를 뽑아 준다. 콘솔이 입력 중에 인자 표를 만드는 데 쓴다.
    `{user_id}`는 시스템이 자동 주입하므로 표에 넣지 않는다."""
    names = [n for n in placeholders_in(body.get("exec_command") or "") if n != "user_id"]
    return {"placeholders": names,
            "has_user_id": "user_id" in placeholders_in(body.get("exec_command") or "")}


async def _validate(body: CommandIn, existing: set[str], tool_name: str):
    args = [a.model_dump() for a in body.args]
    try:
        validate_definition(tool_name, body.exec_command, args, body.host_mode,
                            await _deny(), existing)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return args


@router.post("/commands")
async def create_command(body: CommandIn, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    taken = {r["tool_name"] for r in await pool.fetch("SELECT tool_name FROM execution_commands")}
    taken |= _RESERVED_TOOL_NAMES
    tool_name = tool_name_for(body.title, taken, body.exec_command)
    args = await _validate(body, taken, tool_name)
    try:
        row_id = await pool.fetchval(
            """
            INSERT INTO execution_commands
                (tool_name, title, description, exec_command, args,
                 host_mode, enabled, required_roles, updated_by)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9) RETURNING id
            """,
            tool_name, body.title.strip(), body.description.strip(), body.exec_command.strip(),
            json.dumps(args, ensure_ascii=False), body.host_mode,
            body.enabled, [r.strip() for r in body.required_roles if r.strip()], admin)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"등록 실패 (이름 중복 가능): {e}")
    return {"id": row_id, "tool_name": tool_name, "restart_required": True}


@router.patch("/commands/{command_id}")
async def update_command(command_id: int, body: CommandIn, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    cur = await pool.fetchrow(
        "SELECT tool_name FROM execution_commands WHERE id = $1", command_id)
    if not cur:
        raise HTTPException(404, "커맨드를 찾을 수 없습니다.")
    taken = {r["tool_name"] for r in await pool.fetch(
        "SELECT tool_name FROM execution_commands WHERE id <> $1", command_id)}
    args = await _validate(body, taken | _RESERVED_TOOL_NAMES, cur["tool_name"])
    await pool.execute(
        """
        UPDATE execution_commands
        SET title=$2, description=$3, exec_command=$4, args=$5::jsonb,
            host_mode=$6, enabled=$7, required_roles=$8, updated_by=$9, updated_at=now()
        WHERE id=$1
        """,
        command_id, body.title.strip(), body.description.strip(), body.exec_command.strip(),
        json.dumps(args, ensure_ascii=False), body.host_mode,
        body.enabled, [r.strip() for r in body.required_roles if r.strip()], admin)
    # enabled/역할은 실행 시점에 읽으므로 즉시 반영된다. 나머지는 툴 스키마라 재시작이 필요하다.
    return {"ok": True, "restart_required": True}


@router.patch("/commands/{command_id}/enabled")
async def toggle_command(command_id: int, body: dict, admin: str = Depends(require_admin)):
    """활성/비활성만 바꾼다. **실시간 반영**이라 재시작이 필요 없다."""
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        "UPDATE execution_commands SET enabled=$2, updated_by=$3, updated_at=now() "
        "WHERE id=$1 RETURNING id", command_id, bool(body.get("enabled")), admin)
    if not row:
        raise HTTPException(404, "커맨드를 찾을 수 없습니다.")
    return {"ok": True, "restart_required": False}


@router.delete("/commands/{command_id}")
async def delete_command(command_id: int, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    await pool.execute("DELETE FROM execution_commands WHERE id = $1", command_id)
    return {"ok": True, "restart_required": True}


# ---------------------------------------------------------------- 실행 로그
@router.get("/logs")
async def list_logs(limit: int = 100, admin: str = Depends(require_admin)):
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        "SELECT id, tool_name, params, requested_by, status, result, created_at "
        "FROM job_logs ORDER BY created_at DESC LIMIT $1", max(1, min(int(limit), 500)))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 엑셀/CSV 일괄 등록
@router.post("/commands/excel/preview")
async def preview_excel(
    file: UploadFile | None = File(None),
    server_path: str | None = Form(None),
    strip_html: bool = Form(True),
    collapse_space: bool = Form(True),
    drop_urls: bool = Form(False),
    admin: str = Depends(require_admin),
):
    ext, content, filename = await read_upload_or_server_file(file, server_path, TABLE_EXTS)
    options = {"strip_html": strip_html, "collapse_space": collapse_space, "drop_urls": drop_urls}
    upload_id = await create_upload_session(_DSN, admin, filename, ext,
                                            "execution_commands", content, options)
    session = await get_upload_session(_DSN, upload_id, admin, "execution_commands")
    try:
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
    title_column: str
    description_column: str
    exec_command_column: str | None = None


@router.post("/commands/excel/commit")
async def commit_excel(body: ExcelCommitIn, admin: str = Depends(require_admin)):
    """열 매핑으로 일괄 등록/갱신한다(title 기준 upsert).

    일괄 등록분은 인자 정의 없이 로그인 서버 고정으로 들어간다 - 매뉴얼에서 뽑은 커맨드
    목록은 인자를 미리 알 수 없기 때문이다(인자는 에이전트가 채운다). 필요하면 개별 수정에서
    자리표시자를 넣어 타입을 정의하면 된다.
    """
    session = await get_upload_session(_DSN, body.upload_id, admin, "execution_commands")
    opts = clean_options_from_dict(load_options(session))

    def _build(path: str):
        header, col_idx, rows = load_table_rows(path)
        for label, col in {"이름": body.title_column, "설명": body.description_column}.items():
            if col not in col_idx:
                raise ValueError(f"{label} 열이 파일에 없습니다: {col}")
        if body.exec_command_column and body.exec_command_column not in col_idx:
            raise ValueError(f"존재하지 않는 열입니다: {body.exec_command_column}")

        def _cell(row, col):
            if not col or col not in col_idx:
                return None
            val = row[col_idx[col]]
            return None if val is None else clean_text(str(val), opts)

        built = []
        for row in rows:
            title = _cell(row, body.title_column)
            desc = _cell(row, body.description_column)
            if not title or not desc:
                continue
            exec_command = _cell(row, body.exec_command_column) or title
            built.append((title.strip(), desc, exec_command.strip()))
        return built

    # 실패해도 세션을 지우지 않는다(성공 시에만 정리) - 재시도가 404로 막혀 진짜 원인이 가려진다.
    try:
        items = await run_in_threadpool(_build, session["saved_path"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    await delete_upload_session(_DSN, body.upload_id)
    if not items:
        raise HTTPException(422, "등록할 커맨드가 없습니다. 이름/설명 열 선택을 확인하세요.")

    deny = await _deny()
    pool = await get_pool(_DSN)
    taken = {r["tool_name"] for r in await pool.fetch("SELECT tool_name FROM execution_commands")}
    taken |= _RESERVED_TOOL_NAMES

    inserted = updated = skipped = 0
    problems = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for title, desc, exec_command in items:
                existing = await conn.fetchval(
                    "SELECT tool_name FROM execution_commands WHERE title = $1", title)
                tool_name = existing or tool_name_for(title, taken, exec_command)
                try:
                    # 일괄 등록도 차단 목록을 통과해야 한다(매뉴얼 표에 위험한 줄이 섞일 수 있다).
                    validate_definition(tool_name, exec_command, [], "login_server", deny,
                                        set() if existing else taken)
                except ValueError as e:
                    skipped += 1
                    if len(problems) < 10:
                        problems.append(f"{title}: {e}")
                    continue
                taken.add(tool_name)
                res = await conn.fetchrow(
                    """
                    INSERT INTO execution_commands
                        (tool_name, title, description, exec_command, args,
                         host_mode, enabled, updated_by)
                    VALUES ($1,$2,$3,$4,'[]'::jsonb, 'login_server', true, $5)
                    ON CONFLICT (title) DO UPDATE
                    SET description=EXCLUDED.description, exec_command=EXCLUDED.exec_command,
                        updated_by=EXCLUDED.updated_by, updated_at=now()
                    RETURNING (xmax = 0) AS inserted
                    """, tool_name, title, desc, exec_command, admin)
                if res["inserted"]:
                    inserted += 1
                else:
                    updated += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": len(items), "problems": problems, "restart_required": True}
