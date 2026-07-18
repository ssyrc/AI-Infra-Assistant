"""
ADK 루트 에이전트 빌더.
- LLM/MCP 엔드포인트/시스템 지시문을 전부 config_store(platform_settings)에서 읽는다.
  -> 관리자 콘솔 '설정' 탭에서 값을 바꾸고 이 서비스(agent-server)를 재시작하면 반영된다.
- Tracing: Langfuse (OpenTelemetry). 이건 비밀값이라 config_store가 아니라 .env로 관리한다.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

# --- Langfuse 트레이싱: 앱 임포트 전에 가장 먼저 초기화 ---------------------
# 키가 설정돼 있으면 활성화하고, 없으면(예: dev) 건너뛴다. 트레이싱 실패가
# 에이전트 자체를 죽이지 않도록 방어적으로 처리한다.
if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        from langfuse import Langfuse

        Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "http://langfuse:3000"),
        )
        GoogleADKInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        print(f"[agent] Langfuse 트레이싱 초기화 실패, 트레이싱 없이 계속 진행: {e}")
else:
    print("[agent] LANGFUSE 키가 없어 트레이싱을 비활성화합니다 (dev 모드).")
# --------------------------------------------------------------------------

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from config_store import get_config

APP_NAME = "ops_assistant"

DEFAULT_INSTRUCTION = """\
당신은 사내 시스템 운영/사용을 돕는 어시스턴트입니다.
- 매뉴얼/가이드 질문은 manual MCP의 search_manual 툴로 근거를 찾은 뒤 답변하세요.
- 과거 유사 문의/해결 이력이 필요하면 voc MCP의 search_voc 툴을 사용하세요.
- 사용 가능한 커맨드가 궁금하면 command MCP의 search_commands / get_command_detail을 사용하세요.
- 서버 상태나 job 정보 등 실제 조회가 필요하면 system MCP의 화이트리스트 툴만 사용하세요.
- 근거 없이 추측해서 답변하지 말고, 답변의 출처(문서명/섹션)를 함께 제시하세요.
"""


async def build_agent() -> tuple[Agent, str]:
    """config_store에서 현재 설정값을 읽어 ADK 에이전트를 만든다.
    (MCP 연결은 시작 시점에 한 번 맺으므로, 주소를 바꾸면 이 프로세스를 재시작해야 한다.)"""
    llm_base_url = await get_config("vllm_llm_base_url")
    llm_model = await get_config("vllm_llm_model", "qwen3-32b")
    instruction = await get_config("agent_system_instruction", DEFAULT_INSTRUCTION)

    manual_url = await get_config("manual_mcp_url")
    command_url = await get_config("command_mcp_url")
    voc_url = await get_config("voc_mcp_url")
    system_url = await get_config("system_mcp_url")

    agent = Agent(
        model=LiteLlm(model=f"openai/{llm_model}", api_base=llm_base_url, api_key="not-needed"),
        name="ops_assistant",
        instruction=instruction,
        tools=[
            McpToolset(connection_params=StreamableHTTPConnectionParams(url=manual_url)),
            McpToolset(connection_params=StreamableHTTPConnectionParams(url=command_url)),
            McpToolset(connection_params=StreamableHTTPConnectionParams(url=voc_url)),
            McpToolset(connection_params=StreamableHTTPConnectionParams(url=system_url)),
        ],
    )
    return agent, llm_model
