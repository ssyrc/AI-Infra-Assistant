"""
ADK 루트 에이전트 빌더.
- LLM/MCP 엔드포인트/시스템 지시문을 전부 config_store(platform_settings)에서 읽는다.
- MCP 호출 시 호출자 식별 헤더(X-User-Id 등)를 함께 보내 System MCP 감사로그에 남긴다.
- Tracing: Langfuse (키가 없으면 자동 비활성화).
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

# --- Langfuse 트레이싱: 앱 임포트 전에 가장 먼저 초기화 ---------------------
if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        from langfuse import Langfuse

        Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000"),
        )
        GoogleADKInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        print(f"[agent] Langfuse 트레이싱 초기화 실패, 트레이싱 없이 계속 진행: {e}")
else:
    print("[agent] LANGFUSE 키가 없어 트레이싱을 비활성화합니다.")
# --------------------------------------------------------------------------

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from config_store import get_config

APP_NAME = "ops_assistant"

DEFAULT_INSTRUCTION = """당신은 사내 시스템 운영/사용을 돕는 한국어 어시스턴트입니다.
검색 결과에 근거해서만 답하고, 출처를 함께 제시하세요."""


async def build_agent(caller_headers: dict | None = None,
                      extra_instruction: str | None = None) -> tuple[Agent, str, list[McpToolset]]:
    """config_store의 현재 설정값으로 ADK 에이전트를 만든다.

    caller_headers가 주어지면 MCP 호출에 호출자 식별 헤더(X-User-Id 등)를 함께 보낸다.
    이 헤더는 요청마다 달라지므로(사용자별), 에이전트를 요청 단위로 만든다. System MCP는
    이 헤더로 user_scoped 툴의 user_id를 강제 주입하고 감사로그·권한검사를 수행한다.

    반환하는 toolset 목록은 요청 종료 시 호출자가 close()로 정리한다."""
    llm_base_url = await get_config("vllm_llm_base_url")
    llm_model = await get_config("vllm_llm_model", "qwen3-32b")
    instruction = await get_config("agent_system_instruction", DEFAULT_INSTRUCTION)
    if extra_instruction:
        # 요청별 컨텍스트(예: 사용자 장기 메모리)를 시스템 지시문 뒤에 덧붙인다.
        instruction = f"{instruction}\n{extra_instruction}"

    urls = {
        "manual": await get_config("manual_mcp_url"),
        "command": await get_config("command_mcp_url"),
        "voc": await get_config("voc_mcp_url"),
        "system": await get_config("system_mcp_url"),
    }
    missing = [k for k, v in urls.items() if not v]
    if missing:
        raise RuntimeError(f"MCP 주소가 설정되지 않았습니다: {', '.join(missing)}")

    headers = {k: v for k, v in (caller_headers or {}).items() if v is not None}

    def toolset(url: str) -> McpToolset:
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=url, headers=headers or None))

    toolsets = [toolset(urls["manual"]), toolset(urls["command"]),
                toolset(urls["voc"]), toolset(urls["system"])]
    agent = Agent(
        model=LiteLlm(model=f"openai/{llm_model}", api_base=llm_base_url, api_key="not-needed"),
        name="ops_assistant",
        instruction=instruction,
        tools=list(toolsets),
    )
    return agent, llm_model, toolsets
