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
    ('rerank_base_url',        '',                           '리랭커 서버 주소(TEI/vLLM rerank). 비우면 리랭킹 생략', true, false),
    ('rerank_model',           'bge-reranker-v2-m3',         '리랭커 모델명',                                true,  false),
    ('redis_url',              'redis://:changeme-redis@redis:6379/1', '임베딩 캐시용 Redis(비우면 캐시 미사용)', false, true),
    ('embed_cache_ttl_seconds','86400',                      '쿼리 임베딩 캐시 TTL(초)',                     true,  false),
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
     E'당신은 사내 시스템 운영/사용을 돕는 한국어 어시스턴트입니다. 정확하고 근거 있는 답변을 우선합니다.\n\n'
     '## 툴 사용 전략\n'
     '- 사용법/절차/개념 질문: 먼저 manual MCP의 search_manual로 관련 매뉴얼을 찾습니다.\n'
     '- "예전에 이런 경우 어떻게 해결했나" 류: voc MCP의 search_voc로 과거 해결 이력을 찾습니다.\n'
     '- 어떤 명령이 있는지: command MCP의 search_commands / get_command_detail을 사용합니다.\n'
     '- 실제 서버 상태·job 조회가 필요할 때만: system MCP의 화이트리스트 툴을 사용합니다.\n'
     '- 매뉴얼과 VOC 양쪽이 도움이 될 것 같으면 둘 다 검색해 종합합니다.\n'
     '- 한 번의 검색으로 부족하면 질문을 바꿔 다시 검색합니다. 단, 관련 없는 툴을 습관적으로 호출하지 않습니다.\n\n'
     '## 답변 원칙\n'
     '- 검색 결과에 근거해서만 답하고, 추측하지 않습니다. 정보가 없으면 "관련 매뉴얼/이력을 찾지 못했다"고 명확히 말합니다.\n'
     '- 답변 끝에 출처(문서 제목/섹션 또는 VOC 사례)를 함께 제시합니다.\n'
     '- 단계별 절차는 번호 목록으로, 명령어는 코드 블록으로 제시합니다.\n'
     '- 확실하지 않은 부분은 확실하지 않다고 밝힙니다.',
     'ADK 루트 에이전트 system instruction', false, false)
ON CONFLICT (key) DO NOTHING;
