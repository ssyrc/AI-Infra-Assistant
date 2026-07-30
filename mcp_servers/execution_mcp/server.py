"""
Execution MCP - 커맨드 실행을 담당하는 **단 하나의** MCP. (구 Command MCP + System MCP)

둘로 나눠 뒀던 이유가 없어졌다. 실행 경로가 애초에 같았고(ssh_exec.run_ssh_as_user), 등록은
어느 쪽이든 결국 화이트리스트이며, 인자를 에이전트가 자유롭게 정할 수 있어야 하는 것도 같다.
탭이 둘이라 "이건 어디에 등록하지?"가 매번 생겼고, 재시작 규칙도 감사 로그도 두 벌이었다.

노출하는 툴은 세 갈래다.
  1) **내장 커맨드**(builtin.py) - 값 검증이 필요한 read-only 리눅스 명령 7개.
     `lines` 1~2000, `kind` enum, safe_path() 같은 검사는 템플릿으로 표현할 수 없어 코드로 남겼다.
  2) **등록 커맨드**(execution_commands) - 콘솔에서 `head -n {lines} {path}`처럼 적어 등록한 것.
     자리표시자가 타입 붙은 파라미터로 LLM에 노출된다.
  3) **run_command** - 등록되지 않은 커맨드(매뉴얼/VOC에서 찾았거나 LLM이 아는 것)를 그대로 실행.
     승인한 사람이 없으므로 차단 목록을 **엄격하게** 적용한다(shared/execution_exec).

공통 보장(어느 갈래든 동일):
- 셸을 쓰지 않는다(argv 리스트). 항상 `ssh root@host` 후 `su - <user_id>`로 **호출자 본인**
  권한으로 강등해 실행한다. root로 실행되는 경로가 없다.
- user_id는 LLM 스키마에서 감추고 호출자 헤더에서 강제 주입한다(남의 자원 접근 불가).
- 전건 감사 로그(job_logs).
"""
import sys
import os
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from db import get_pool  # noqa: E402
from config_store import get_config  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, load_overrides_sync, tool_description, build_wrapped,
)
from ssh_exec import (  # noqa: E402
    master_alive, run_ssh_as_user, set_output_limit_getter, warm_master,
    start_master_keepalive,
)
from execution_exec import DEFAULT_DENY_CSV, build_free_argv, deny_set  # noqa: E402
from builtin import BUILTIN_COMMANDS  # noqa: E402
from registry import (  # noqa: E402
    estimate_prompt_tokens, load_registered_sync, set_deny_csv_getter,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("execution-mcp", stateless_http=True, host="0.0.0.0")

_DSN = "execution_db_dsn"
_STATE = "execution_builtin_state"


async def _deny_csv() -> str:
    return await get_config("execution_deny_commands", DEFAULT_DENY_CSV)


set_deny_csv_getter(_deny_csv)


async def _output_limit() -> int:
    """커맨드 출력을 LLM에 넘길 때의 상한. 매뉴얼·VOC 결과(건당 1500자)와 같은 이유로 필요하다 -
    출력이 그대로 다음 요청 프롬프트에 실린다(#123)."""
    return int(await get_config("execution_result_max_chars", "4000"))


set_output_limit_getter(_output_limit)


async def _login_host() -> str:
    return await get_config("scheduler_login_host", "202.20.185.100")


# ------------------------------------------------------------------ 미등록 커맨드 실행
async def run_command(user_id: str, command: str, args: list[str] | None = None,
                      host: str | None = None) -> dict:
    """등록되지 않은 커맨드를 그대로 실행한다(매뉴얼/VOC에서 찾은 것 등).

    등록 커맨드는 각각 전용 툴로 이미 노출돼 있으므로 이 툴을 거치지 않는다.
    여기로 오는 것은 아무도 승인하지 않은 문자열이라, 차단 목록을 모든 토큰에 엄격히 적용한다
    (`mpirun -n 4 rm -rf /`, `bash -c "..."` 같은 우회를 막기 위함).
    """
    deny = deny_set(await _deny_csv())
    argv = build_free_argv(command, args, user_id, deny)
    target = (host or "").strip() or await _login_host()
    result = await run_ssh_as_user(target, user_id, argv)
    result["source"] = "미등록(run_command)"
    return result


FREE_TOOLS = {
    "run_command": {
        "handler": run_command,
        "description": (
            "**등록되지 않은** 사내 커맨드를 실행한다. 등록된 커맨드는 각각 전용 툴로 나와 있으니 "
            "그 툴을 먼저 쓰고, 목록에 없는 커맨드(매뉴얼에서 찾은 것 등)만 이 툴로 실행한다. "
            "args는 인자를 한 칸씩 나눠 넣는다(예: ['-l','/home']). "
            "host는 사용자가 특정 서버를 지목했을 때만 넣는다(기본: 로그인 서버). "
            "파괴적이거나 다른 명령을 대신 실행하는 커맨드는 거부된다."
        ),
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
}


# ------------------------------------------------------------------ 상태(활성/역할) 조회
async def _log_execution(tool_name: str, params: dict, status: str, result):
    caller = get_caller()
    pool = await get_pool(_DSN)
    await pool.execute(
        """
        INSERT INTO job_logs (tool_name, params, requested_by, status, result,
                              conversation_id, request_id)
        VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6, $7)
        """,
        tool_name,
        json.dumps(params, ensure_ascii=False, default=str),
        caller.get("user_id") or "unknown",
        status,
        json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
        caller.get("conversation_id"),
        caller.get("request_id"),
    )


async def _is_enabled(tool_name: str, default: bool) -> bool:
    """활성 여부는 **실행 시점에** 읽는다(콘솔에서 끄면 재시작 없이 바로 막힌다)."""
    pool = await get_pool(_DSN)
    if tool_name in BUILTIN_COMMANDS:
        row = await pool.fetchrow(
            f"SELECT enabled FROM {_STATE} WHERE tool_name = $1", tool_name)
        if row is None:
            # host_mode는 **넣지 않는다**. 여기서 값을 채우면 컬럼 기본값이 코드가 지정한
            # 실행 위치(login_server)를 덮어써서, host가 LLM에 노출되고 엉뚱한 서버에서
            # 실행된다(#115). NULL로 두면 builtin.py의 값이 쓰인다.
            await pool.execute(
                f"INSERT INTO {_STATE} (tool_name, enabled) VALUES ($1, $2) "
                "ON CONFLICT (tool_name) DO NOTHING", tool_name, default)
            return default
        return row["enabled"]
    if tool_name in FREE_TOOLS:
        return True
    row = await pool.fetchrow(
        "SELECT enabled FROM execution_commands WHERE tool_name = $1", tool_name)
    return default if row is None else row["enabled"]


async def _required_roles(tool_name: str, code_default: list) -> list:
    pool = await get_pool(_DSN)
    table = _STATE if tool_name in BUILTIN_COMMANDS else "execution_commands"
    if tool_name in FREE_TOOLS:
        return []
    row = await pool.fetchrow(
        f"SELECT required_roles FROM {table} WHERE tool_name = $1", tool_name)
    if row and row["required_roles"] is not None:
        return list(row["required_roles"])
    return list(code_default or [])


# ------------------------------------------------------------------ 툴 구성 (기동 시 1회)
# host_mode와 설명은 LLM 스키마/프롬프트에 영향을 줘서 기동 시에만 반영한다(변경 후 재시작 필요).
_OVERRIDES = load_overrides_sync(_DSN, _STATE, extra_columns=("host_mode", "enabled"))

# 비활성 내장 커맨드도 툴 목록에서 뺀다(등록 커맨드와 같은 이유 - 프롬프트 예산).
# 끄는 즉시 막히는 건 _is_enabled가 호출 시점에 또 확인하므로 그대로다.
BUILTIN, _OFF = {}, 0
for _name, _e in BUILTIN_COMMANDS.items():
    _ov = _OVERRIDES.get(_name) or {}
    if _ov.get("enabled") is False:
        _OFF += 1
        continue
    BUILTIN[_name] = {**_e, "host_mode": _ov["host_mode"]} if _ov.get("host_mode") else _e
if _OFF:
    print(f"[execution-mcp] 비활성 내장 커맨드 {_OFF}개는 툴 목록에서 제외했습니다.")

REGISTERED, _DROPPED = load_registered_sync(_login_host)
if _DROPPED:
    print(f"[execution-mcp] 등록 커맨드가 상한(execution_tools_max)을 넘어 {_DROPPED}개를 툴로 "
          "노출하지 못했습니다. 설정에서 상한을 올리거나 쓰지 않는 커맨드를 정리하세요 "
          "- 노출되지 않은 커맨드는 run_command로만 실행할 수 있습니다.")

ALL_TOOLS = {**BUILTIN, **REGISTERED, **FREE_TOOLS}
_CHARS, _TOKENS = estimate_prompt_tokens(
    [tool_description(n, e, _OVERRIDES) for n, e in ALL_TOOLS.items()])
# 툴 설명은 **매 요청** 프롬프트에 통째로 실린다. 컨텍스트가 32768이라 등록이 늘수록
# 검색 결과·대화 이력에 쓸 자리가 줄어든다. 실제 비용을 찍어 두면 감이 아니라 숫자로 판단할 수 있다.
print(f"[execution-mcp] 내장 {len(BUILTIN)}개 · 등록 {len(REGISTERED)}개 · run_command 1개 "
      f"= 툴 {len(ALL_TOOLS)}개 (스키마 {_CHARS:,}자 ≈ {_TOKENS:,}토큰/요청)")
if _TOKENS > 8000:
    print(f"[execution-mcp] 경고: 툴 스키마가 요청마다 약 {_TOKENS:,}토큰을 씁니다. 지시문(~4.9k)과 "
          "합치면 32768 컨텍스트의 절반에 가까워 검색 결과·대화 이력이 밀려납니다. "
          "커맨드 설명을 한 줄로 줄이거나 안 쓰는 커맨드를 정리하세요.")

for _name, _entry in ALL_TOOLS.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, is_enabled=_is_enabled,
                      required_roles=_required_roles, log_execution=_log_execution,
                      host_mode=_entry.get("host_mode", "target_server"),
                      login_host=_login_host),
        name=_name,
        description=tool_description(_name, _entry, _OVERRIDES),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8002))
    inner = mcp.streamable_http_app()

    # 기동하자마자 로그인 서버로 ssh 마스터 연결을 열어 둔다. 사용자가 첫 질문을 던질 때
    # 이미 연결이 서 있으므로 커맨드가 곧바로 실행된다(첫 접속 비용이 체감 지연의 대부분).
    async def _warm_ssh():
        try:
            host = await _login_host()
        except Exception as e:  # noqa: BLE001
            print(f"[execution-mcp] 로그인 서버 설정을 읽지 못해 예열을 건너뜁니다: {e}")
            return
        if host:
            ok = await warm_master(host)
            alive = await master_alive(host) if ok else False
            # "ssh 세션이 제대로 열렸는지"를 기동 로그에서 바로 확인할 수 있게 남긴다.
            if alive:
                print(f"[execution-mcp] ssh 다중화 마스터 준비 완료({host}). "
                      "첫 커맨드부터 곧바로 실행됩니다.")
            else:
                print(f"[execution-mcp] ssh 마스터를 열지 못했습니다({host}). 커맨드는 실행되지만 "
                      "매번 새로 접속해 1~3초씩 더 걸립니다. "
                      "scripts/diag-ssh.sh 로 원인을 확인하세요.")
        start_master_keepalive(lambda: get_config("scheduler_login_host", host))

    inner.add_event_handler("startup", _warm_ssh)
    uvicorn.run(CallerContextMiddleware(inner), host="0.0.0.0", port=port)
