\c platform_config

CREATE TABLE IF NOT EXISTS platform_settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    description  TEXT,
    hot_reload   BOOLEAN NOT NULL DEFAULT false,  -- true: 재시작 없이 즉시 반영 / false: 서비스 재시작 필요
    is_secret    BOOLEAN NOT NULL DEFAULT false,  -- true: 관리자 콘솔에서 값 마스킹
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO platform_settings (key, value, description, hot_reload, is_secret) VALUES
    ('vllm_llm_base_url',      'http://CHANGE-ME:8000/v1',  'vLLM LLM 서버 주소 (OpenAI 호환)',            true,  false),
    ('vllm_llm_model',         'qwen3-32b',                  'vLLM에 서빙 중인 LLM 모델명',                  true,  false),
    ('vllm_embed_base_url',    'http://CHANGE-ME:8010/v1',  'vLLM 임베딩 서버 주소',                        true,  false),
    ('vllm_embed_model',       'bge-m3',                     '임베딩 모델명',                                true,  false),
    ('scheduler_api_base_url', 'http://s2-scheduler:9000',   'System MCP가 호출하는 s2 스케줄러 API 주소',   true,  false),

    ('manual_db_dsn',  'postgresql://agent:changeme@postgres:5432/manual_db',  'Manual MCP 전용 DB',  false, true),
    ('voc_db_dsn',     'postgresql://agent:changeme@postgres:5432/voc_db',     'VOC MCP 전용 DB',     false, true),
    ('command_db_dsn', 'postgresql://agent:changeme@postgres:5432/command_db', 'Command MCP 전용 DB', false, true),
    ('system_db_dsn',  'postgresql://agent:changeme@postgres:5432/system_db',  'System MCP 전용 DB',  false, true),
    ('agent_session_db_dsn',
     'postgresql+asyncpg://agent:changeme@postgres:5432/agent_sessions_db',
     'ADK DatabaseSessionService용 DB (에이전트 서버 여러 대를 띄워도 세션 공유). asyncpg 드라이버 스킴(postgresql+asyncpg://) 필수',
     false, true),

    ('manual_mcp_url',  'http://manual-mcp:8001/mcp',  'Agent Server가 연결할 Manual MCP 주소',  false, false),
    ('command_mcp_url', 'http://command-mcp:8002/mcp', 'Agent Server가 연결할 Command MCP 주소', false, false),
    ('voc_mcp_url',     'http://voc-mcp:8003/mcp',     'Agent Server가 연결할 VOC MCP 주소',     false, false),
    ('system_mcp_url',  'http://system-mcp:8004/mcp',  'Agent Server가 연결할 System MCP 주소',  false, false),

    ('agent_system_instruction',
     E'당신은 사내 시스템 운영/사용을 돕는 어시스턴트입니다.\n'
     '- 매뉴얼/가이드 질문은 manual MCP의 search_manual 툴로 근거를 찾은 뒤 답변하세요.\n'
     '- 과거 유사 문의/해결 이력이 필요하면 voc MCP의 search_voc 툴을 사용하세요.\n'
     '- 사용 가능한 커맨드가 궁금하면 command MCP의 search_commands / get_command_detail을 사용하세요.\n'
     '- 서버 상태나 job 정보 등 실제 조회가 필요하면 system MCP의 화이트리스트 툴만 사용하세요.\n'
     '- 근거 없이 추측해서 답변하지 말고, 답변의 출처(문서명/섹션)를 함께 제시하세요.',
     'ADK 루트 에이전트 system instruction', false, false)
ON CONFLICT (key) DO NOTHING;
