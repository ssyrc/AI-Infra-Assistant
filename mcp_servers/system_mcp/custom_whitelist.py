"""
system_custom_commands(관리자 콘솔에서 등록한 커맨드)를 WHITELIST 항목 형태로 변환해
System MCP 기동 시 병합한다. 코드 배포 없이 추가되지만, 실행 경로(ssh_exec.run_ssh_as_user,
argv 리스트, user_id 강제 주입)는 코드 내장 화이트리스트와 완전히 동일하다.

기동 시 1회만 읽는다(코드 내장 항목의 description_override와 동일한 제약) - 새로 추가/수정한
커맨드를 반영하려면 System MCP 재시작이 필요하다.
"""
import asyncio
import inspect
import json
import os
import sys

import asyncpg

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from ssh_exec import run_ssh_as_user  # noqa: E402
from custom_commands import PARAM_TYPES, render_argv  # noqa: E402


def _build_entry(row: dict) -> dict:
    argv_template = row["argv_template"]
    params = row["params"] or []

    async def handler(user_id: str, host: str, **kwargs) -> dict:
        values = {}
        for p in params:
            name = p["name"]
            caster = PARAM_TYPES.get(p.get("type", "str"), str)
            values[name] = caster(kwargs[name])
        return await run_ssh_as_user(host, user_id, render_argv(argv_template, values))

    sig_params = [
        inspect.Parameter("user_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        inspect.Parameter("host", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
    ]
    annotations = {"user_id": str, "host": str}
    for p in params:
        t = PARAM_TYPES.get(p.get("type", "str"), str)
        sig_params.append(inspect.Parameter(p["name"], inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=t))
        annotations[p["name"]] = t
    handler.__signature__ = inspect.Signature(sig_params)
    handler.__annotations__ = annotations
    handler.__name__ = row["tool_name"]
    handler.__doc__ = row["description"]

    return {
        "handler": handler,
        "description": row["description"],
        "enabled": row["enabled"],
        "required_roles": list(row.get("required_roles") or []),
        "user_scoped": True,
        "scope_param": "user_id",
    }


def load_custom_whitelist_sync(dsn_key: str = "system_db_dsn") -> dict:
    """기동 시 1회, system_custom_commands를 읽어 WHITELIST 형태의 dict로 돌려준다."""
    async def _run() -> dict:
        config_dsn = os.environ.get("CONFIG_DB_DSN")
        if not config_dsn:
            return {}
        conn = await asyncpg.connect(config_dsn)
        try:
            dsn = await conn.fetchval("SELECT value FROM platform_settings WHERE key = $1", dsn_key)
        finally:
            await conn.close()
        if not dsn:
            return {}
        c2 = await asyncpg.connect(dsn)
        try:
            rows = await c2.fetch(
                "SELECT tool_name, description, argv_template, params, required_roles, enabled "
                "FROM system_custom_commands"
            )
        finally:
            await c2.close()
        result = {}
        for r in rows:
            row = dict(r)
            row["argv_template"] = json.loads(row["argv_template"])
            row["params"] = json.loads(row["params"])
            result[row["tool_name"]] = _build_entry(row)
        return result

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[mcp] 콘솔 등록 커맨드 로드 실패, 무시하고 기동: {type(e).__name__}: {e}")
        return {}
