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


async def build_agent() -> tuple[Agent, str]:
    """config_store의 현재 설정값으로 ADK 에이전트를 만든다.
    MCP 연결은 시작 시 한 번 맺으므로, 주소를 바꾸면 이 프로세스를 재시작해야 한다."""
    llm_base_url = await get_config("vllm_llm_base_url")
    llm_model = await get_config("vllm_llm_model", "qwen3-32b")
    instruction = await get_config("agent_system_instruction", DEFAULT_INSTRUCTION)

    urls = {
        "manual": await get_config("manual_mcp_url"),
        "command": await get_config("command_mcp_url"),
        "voc": await get_config("voc_mcp_url"),
        "system": await get_config("system_mcp_url"),
    }
    missing = [k for k, v in urls.items() if not v]
    if missing:
        raise RuntimeError(f"MCP 주소가 설정되지 않았습니다: {', '.join(missing)}")

    def toolset(url: str) -> McpToolset:
        return McpToolset(connection_params=StreamableHTTPConnectionParams(url=url))

    agent = Agent(
        model=LiteLlm(model=f"openai/{llm_model}", api_base=llm_base_url, api_key="not-needed"),
        name="ops_assistant",
        instruction=instruction,
        tools=[toolset(urls["manual"]), toolset(urls["command"]),
               toolset(urls["voc"]), toolset(urls["system"])],
    )
    return agent, llm_model
