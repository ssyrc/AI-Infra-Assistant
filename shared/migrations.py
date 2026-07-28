"""
DB 마이그레이션 + 설정 부트스트랩 러너.

해결하는 문제:
1) credential을 SQL 시드에 하드코딩하지 않는다. DB/Redis 접속 정보는 환경변수에서 읽어
   platform_settings에 주입하므로, POSTGRES_PASSWORD를 바꿔도 DSN이 자동으로 맞춰진다.
2) init-db/*.sql은 Postgres 최초 기동에만 실행되므로, 이후 추가되는 스키마 변경/신규 설정 키가
   기존 DB에 반영되지 않는다. 여기서 버전별 마이그레이션을 매 기동 시 멱등하게 적용한다.

실행: compose의 db-init 원샷 서비스가 다른 서비스보다 먼저 실행한다.
      python -m migrations  (또는 python migrations.py)
"""
import os
import asyncio
import asyncpg

from catalog_exec import DEFAULT_DENY_CSV

PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
PG_USER = os.environ.get("POSTGRES_USER", "agent")
PG_PASSWORD = os.environ["POSTGRES_PASSWORD"]

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_CACHE_DB = os.environ.get("REDIS_CACHE_DB", "1")

APP_DBS = ["platform_config", "manual_db", "voc_db", "command_db", "system_db",
           "agent_sessions_db", "memory_db", "langfuse"]


def dsn(db: str) -> str:
    return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{db}"


def redis_url() -> str:
    if not REDIS_HOST:
        return ""
    auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
    return f"redis://{auth}{REDIS_HOST}:{REDIS_PORT}/{REDIS_CACHE_DB}"


# --- 버전별 마이그레이션 ---------------------------------------------------------
# (db, version, sql). 같은 (db, version)은 한 번만 적용된다.
# 새 변경은 반드시 새 version을 추가하는 방식으로만 넣는다(기존 항목 수정 금지).
MIGRATIONS: list[tuple[str, int, str]] = [
    ("platform_config", 1, """
        CREATE TABLE IF NOT EXISTS platform_settings (
            key          TEXT PRIMARY KEY,
            value        TEXT NOT NULL,
            description  TEXT,
            hot_reload   BOOLEAN NOT NULL DEFAULT false,
            is_secret    BOOLEAN NOT NULL DEFAULT false,
            updated_by   TEXT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # 관리자 콘솔 계정 관리(.env의 ADMIN_USER는 잠금 방지용 기본 계정으로 항상 별도 유효,
    # 여기 등록된 계정은 그 외 추가 관리자용). 비밀번호는 bcrypt 해시로만 저장한다.
    ("platform_config", 2, """
        CREATE TABLE IF NOT EXISTS admin_accounts (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_by    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    ("manual_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS manual_files (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'document',
            uploaded_by TEXT,
            uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            version INT NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft'
        );
        CREATE TABLE IF NOT EXISTS manual_chunks (
            id SERIAL PRIMARY KEY,
            manual_file_id INT REFERENCES manual_files(id) ON DELETE CASCADE,
            seq INT NOT NULL DEFAULT 0,
            section_title TEXT,
            page_no INT,
            chunk_text TEXT NOT NULL,
            embedding vector(1024),
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text, ''))) STORED
        );
        CREATE INDEX IF NOT EXISTS manual_chunks_embedding_idx ON manual_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
    """),
    ("voc_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS voc_records (
            id SERIAL PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            resolved BOOLEAN NOT NULL DEFAULT true,
            department TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            embedding vector(1024),
            tsv tsvector GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(question, '') || ' ' || coalesce(answer, ''))
            ) STORED
        );
        CREATE INDEX IF NOT EXISTS voc_records_embedding_idx ON voc_records USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS voc_records_tsv_idx ON voc_records USING gin (tsv);
    """),
    ("command_db", 1, """
        CREATE TABLE IF NOT EXISTS command_catalog (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL,
            usage TEXT,
            category TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    ("system_db", 1, """
        CREATE TABLE IF NOT EXISTS system_whitelist_state (
            tool_name TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT true,
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS job_logs (
            id SERIAL PRIMARY KEY,
            tool_name TEXT NOT NULL,
            params JSONB,
            requested_by TEXT,
            status TEXT,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # v2: 업로드 세션을 서버가 관리 (클라이언트가 경로/옵션을 결정하지 못하게)
    ("manual_db", 2, """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            saved_path  TEXT NOT NULL,
            kind        TEXT NOT NULL,          -- document | spreadsheet
            options     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # v3: 감사로그에 사용자/대화 식별자 추가
    ("system_db", 3, """
        ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS conversation_id TEXT;
        ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS request_id TEXT;
        CREATE INDEX IF NOT EXISTS job_logs_created_idx ON job_logs (created_at DESC);
    """),
    # v4: 임베딩 모델 메타데이터 (모델 변경 시 재임베딩 판단용)
    ("manual_db", 4, """
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS embed_dim INT;
    """),
    ("voc_db", 4, """
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS embed_dim INT;
    """),
    # v2: 커맨드 카탈로그를 의미 검색(임베딩+FTS 하이브리드) 대상으로 승격.
    #     사용자가 "완전 일치" 키워드가 아니라 설명형으로 물어도 적절한 커맨드를 찾게 한다.
    ("command_db", 2, """
        CREATE EXTENSION IF NOT EXISTS vector;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embedding vector(1024);
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embed_dim INT;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(name, '') || ' ' || coalesce(description, '') || ' ' || coalesce(usage, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS command_catalog_embedding_idx
            ON command_catalog USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS command_catalog_tsv_idx
            ON command_catalog USING gin (tsv);
    """),
    # v3: 커맨드 탭도 엑셀 업로드 미리보기 세션을 사용한다(매뉴얼과 동일한 보안 모델).
    ("command_db", 3, """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            saved_path  TEXT NOT NULL,
            kind        TEXT NOT NULL,
            options     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS command_upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # v4: 화이트리스트 설명/권한을 관리자 콘솔에서 편집할 수 있게 오버라이드 컬럼 추가.
    #     required_roles는 실행 시점에 실시간 반영, description_override는 MCP 재시작 시 반영.
    ("system_db", 4, """
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS required_roles TEXT[] NOT NULL DEFAULT '{}';
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS description_override TEXT;
    """),
    # v4: 스케줄러 실행 툴이 System에서 Command로 이동. Command도 실행형 MCP가 되므로
    #     활성/역할/설명 오버라이드 상태 테이블과 감사로그를 둔다(System과 동일 구조).
    ("command_db", 4, """
        CREATE TABLE IF NOT EXISTS command_whitelist_state (
            tool_name TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT true,
            required_roles TEXT[] NOT NULL DEFAULT '{}',
            description_override TEXT,
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS job_logs (
            id SERIAL PRIMARY KEY,
            tool_name TEXT NOT NULL,
            params JSONB,
            requested_by TEXT,
            status TEXT,
            result JSONB,
            conversation_id TEXT,
            request_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS command_job_logs_created_idx ON job_logs (created_at DESC);
    """),
    # 사용자별 장기 메모리(단일 user_id 키). 대화 턴 원장 + 증류된 장기기억 + 대화 상태.
    # 상위 agent(예: 통합 VOC)에서 오는 요청도 이 메모리를 공유한다.
    ("memory_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS memory_turns (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            conversation_id TEXT,
            source TEXT,
            role TEXT NOT NULL,                 -- 'user' | 'assistant'
            content TEXT NOT NULL,
            embedding vector(1024),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS memory_turns_conv_idx ON memory_turns (conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS memory_turns_user_idx ON memory_turns (user_id, created_at);
        CREATE INDEX IF NOT EXISTS memory_turns_emb_idx ON memory_turns USING hnsw (embedding vector_cosine_ops);

        -- 여러 대화에서 증류된 사용자 장기기억(사실/선호/요약). user_id 단위로 공유.
        CREATE TABLE IF NOT EXISTS user_memory (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',  -- 'fact' | 'preference' | 'summary'
            content TEXT NOT NULL,
            embedding vector(1024),
            source TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS user_memory_user_idx ON user_memory (user_id);
        CREATE INDEX IF NOT EXISTS user_memory_emb_idx ON user_memory USING hnsw (embedding vector_cosine_ops);

        -- 대화별 요약 진행 상태(어디까지 요약해 승격했는지).
        CREATE TABLE IF NOT EXISTS conversation_state (
            conversation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            turn_count INT NOT NULL DEFAULT 0,
            summarized_upto BIGINT NOT NULL DEFAULT 0,   -- 이 memory_turns.id 이하까지 요약 완료
            last_summarized_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # 관리자 콘솔에서 코드 배포 없이 새 System MCP 화이트리스트 커맨드를 등록할 수 있게 한다.
    # argv_template의 "{param}" 토큰이 params에 정의된 파라미터로 치환된다(셸 미사용, argv 그대로 실행).
    # 항상 user_id로 scope(호출자 권한 강제)되고 host가 필수라 기존 화이트리스트 항목과 안전모델이 같다.
    ("system_db", 5, """
        CREATE TABLE IF NOT EXISTS system_custom_commands (
            tool_name      TEXT PRIMARY KEY,
            description    TEXT NOT NULL,
            argv_template  JSONB NOT NULL,
            params         JSONB NOT NULL DEFAULT '[]',
            required_roles TEXT[] NOT NULL DEFAULT '{}',
            enabled        BOOLEAN NOT NULL DEFAULT false,
            created_by     TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by     TEXT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # v6: host 파라미터를 "해당 서버 실행"(LLM이 서버명을 지정)과 "로그인 서버 실행"
    #     (게이트/로그인 서버로 고정, LLM 스키마에서 host 자체를 숨김) 중 하나로 분류.
    #     화이트리스트/커스텀 커맨드 둘 다 같은 개념이라 두 테이블에 동일하게 추가한다.
    #     스키마(LLM에 보이는 파라미터)에 영향을 주므로 변경 후 System MCP 재시작이 필요하다.
    ("system_db", 6, """
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS host_mode TEXT NOT NULL DEFAULT 'target_server';
        ALTER TABLE system_whitelist_state
            ADD CONSTRAINT system_whitelist_state_host_mode_check
            CHECK (host_mode IN ('target_server', 'login_server'));
        ALTER TABLE system_custom_commands
            ADD COLUMN IF NOT EXISTS host_mode TEXT NOT NULL DEFAULT 'target_server';
        ALTER TABLE system_custom_commands
            ADD CONSTRAINT system_custom_commands_host_mode_check
            CHECK (host_mode IN ('target_server', 'login_server'));
    """),
    # v5: 카탈로그(매뉴얼 엑셀 업로드본)에 등록된 커맨드를 그대로 실행할 수 있게 한다.
    #     exec_command = 실제 실행할 커맨드 문자열(셸 없이 shlex 분해 후 argv로 실행).
    #     비어 있으면 name을 그대로 실행한다 -> 기존에 올린 카탈로그도 추가 작업 없이 실행 가능.
    ("command_db", 5, """
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS exec_command TEXT;
    """),
]


# --- 설정 시드 -------------------------------------------------------------------
# credential류는 환경변수에서 만들어 넣는다(SQL에 하드코딩하지 않음).
# force=True인 항목은 매 기동 시 환경변수 값으로 덮어써서 비밀번호 변경이 자동 반영되게 한다.
def config_seed() -> list[tuple[str, str, str, bool, bool, bool]]:
    """(key, value, description, hot_reload, is_secret, force)"""
    return [
        # 주소류 기본값은 .env(환경변수)에서 읽는다 -> 배포 시 주소를 .env 한 곳에서 관리.
        # force=False라 최초 1회만 주입되고, 이후 관리자 콘솔에서 바꾼 값을 덮어쓰지 않는다.
        ("vllm_llm_base_url", os.environ.get("VLLM_LLM_BASE_URL", "http://CHANGE-ME:8000/v1"), "vLLM LLM 서버 주소 (OpenAI 호환)", True, False, False),
        ("vllm_llm_model", os.environ.get("VLLM_LLM_MODEL", "qwen3-32b"), "vLLM에 서빙 중인 LLM 모델명", True, False, False),
        ("vllm_embed_base_url", os.environ.get("VLLM_EMBED_BASE_URL", "http://CHANGE-ME:8010/v1"), "vLLM 임베딩 서버 주소", True, False, False),
        ("vllm_embed_model", os.environ.get("VLLM_EMBED_MODEL", "bge-m3"), "임베딩 모델명", True, False, False),
        ("embed_dim", os.environ.get("EMBED_DIM", "1024"), "임베딩 차원(스키마 vector(N)과 일치해야 함)", False, False, False),
        ("rerank_provider", os.environ.get("RERANK_PROVIDER", "tei"), "리랭커 종류: tei | vllm | none", True, False, False),
        ("rerank_base_url", os.environ.get("RERANK_BASE_URL", ""), "리랭커 서버 주소. 비우면 리랭킹 생략", True, False, False),
        ("rerank_model", os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3"), "리랭커 모델명", True, False, False),
        ("rerank_timeout_seconds", "5", "리랭커 타임아웃(초). 초과 시 RRF 결과로 fallback", True, False, False),
        ("embed_cache_ttl_seconds", "86400", "쿼리 임베딩 캐시 TTL(초)", True, False, False),
        ("clean_policy_version", "1", "정제 정책 버전(캐시 키에 포함)", True, False, False),
        ("search_max_top_k", "20", "검색 top_k 상한", True, False, False),
        ("search_max_candidates", "100", "리랭킹 후보 상한", True, False, False),
        ("upload_max_mb", "50", "업로드 최대 크기(MB)", True, False, False),
        ("upload_session_ttl_minutes", "60", "업로드 미리보기 세션 유효시간(분)", True, False, False),
        ("upload_source_dir", "/data/uploads",
         "매뉴얼/VOC/커맨드 카탈로그 '서버 파일에서 선택' 목록 경로(admin-console 컨테이너 내부 "
         "경로, docker-compose에서 마운트된 폴더 하위만 가능)", True, False, False),
        ("scheduler_login_host", os.environ.get("SCHEDULER_LOGIN_HOST", "login05"), "Command MCP가 job 조회 시 ssh할 로그인 서버(/etc/hosts 등록명)", True, False, False),
        # 커맨드 카탈로그는 전부 실행 가능하다(항목별 on/off 화이트리스트는 System MCP에서만 관리).
        # 그래도 파괴적인 기본 명령은 실행 시점에 거부한다. 콤마 구분, 비우면 제한 없음.
        ("catalog_exec_deny_commands", DEFAULT_DENY_CSV,
         "커맨드 카탈로그 실행 시 거부할 기본 명령(콤마 구분). 비우면 제한 없이 전부 실행됨", True, False, False),

        # 장기 메모리(사용자별)
        ("memory_enabled", "true", "장기 메모리 사용 여부(true/false)", True, False, False),
        ("memory_recent_turns", "8", "프롬프트에 주입할 최근 대화 턴 수", True, False, False),
        ("memory_top_k", "5", "장기기억에서 의미검색으로 주입할 최대 항목 수", True, False, False),
        ("memory_summarize_every", "12", "이 턴 수마다 오래된 대화를 요약해 장기기억으로 승격", True, False, False),
        ("memory_ttl_days", "180", "장기기억 보존일(0이면 무기한)", True, False, False),

        # credential류: 환경변수 기반, 매 기동 시 갱신(force=True)
        ("manual_db_dsn", dsn("manual_db"), "Manual MCP 전용 DB", False, True, True),
        ("voc_db_dsn", dsn("voc_db"), "VOC MCP 전용 DB", False, True, True),
        ("command_db_dsn", dsn("command_db"), "Command MCP 전용 DB", False, True, True),
        ("system_db_dsn", dsn("system_db"), "System MCP 전용 DB", False, True, True),
        ("agent_session_db_dsn",
         dsn("agent_sessions_db").replace("postgresql://", "postgresql+asyncpg://"),
         "ADK DatabaseSessionService용 DB (asyncpg 스킴)", False, True, True),
        ("memory_db_dsn", dsn("memory_db"), "사용자별 장기 메모리 DB", False, True, True),
        ("redis_url", redis_url(), "임베딩 캐시용 Redis(비우면 캐시 미사용)", False, True, True),

        ("manual_mcp_url", os.environ.get("MANUAL_MCP_URL", "http://manual-mcp:8001/mcp"),
         "Agent Server가 연결할 Manual MCP 주소", False, False, False),
        ("command_mcp_url", os.environ.get("COMMAND_MCP_URL", "http://command-mcp:8002/mcp"),
         "Agent Server가 연결할 Command MCP 주소", False, False, False),
        ("voc_mcp_url", os.environ.get("VOC_MCP_URL", "http://voc-mcp:8003/mcp"),
         "Agent Server가 연결할 VOC MCP 주소", False, False, False),
        ("system_mcp_url", os.environ.get("SYSTEM_MCP_URL", "http://system-mcp:8004/mcp"),
         "Agent Server가 연결할 System MCP 주소", False, False, False),
        ("service_hub_mcp_url", os.environ.get("SERVICE_HUB_MCP_URL", ""),
         "유사 VOC 조회용 Service Hub MCP 주소(비우면 similar_voc 생략). 방화벽 개통 후 설정", True, False, False),
        ("voc_similar_top_k", "3", "VOC 답변에 붙일 유사 VOC 최대 개수(0이면 비활성)", True, False, False),

        # Open WebUI 기본 모델 동기화("설정" 탭의 "Open WebUI 기본 모델 동기화" 버튼용).
        # API 키는 Open WebUI 관리자 계정으로 로그인 -> 설정 -> 계정 -> API 키에서 발급.
        ("openwebui_base_url", os.environ.get("OPENWEBUI_BASE_URL", "http://open-webui:8080"),
         "Open WebUI 내부 주소(기본 모델 동기화용)", True, False, False),
        ("openwebui_admin_api_key", "", "Open WebUI 관리자 API 키(기본 모델 동기화용, 비우면 동기화 생략)", True, True, False),

        ("agent_system_instruction", AGENT_INSTRUCTION, "ADK 루트 에이전트 system instruction", False, False, False),
    ]


AGENT_INSTRUCTION = """당신은 사내 인프라/시스템 운영을 돕는 한국어 어시스턴트(AI Infra Assistant)입니다.

# 답변 방식 (가장 중요 — 어기지 않는다)
1. **진행 상황을 중계하지 않습니다.** "확인해 드리겠습니다", "검색해 보겠습니다", "매뉴얼에서 찾았습니다", "실행하겠습니다" 같은 문장을 쓰지 않습니다. 도구는 조용히 호출하고, **최종 결과만** 답합니다.
2. 도구를 여러 번 호출해도 사용자에게는 **마지막 답 하나만** 보입니다. 중간 생각은 겉으로 쓰지 않습니다.
3. **출처를 표시하지 않습니다.** "출처: ...", "매뉴얼 문서 ...에 따르면" 같은 꼬리말을 붙이지 않습니다.
4. 실행 결과는 **결과 자체만** 간결히 보여줍니다. 결과 수치/출력을 그대로 옮기고, 필요하면 한두 줄로 요약합니다.
5. 커맨드가 실패하면 **실패했다는 사실과 오류 메시지만** 짧게 전합니다. 원인을 추측하지 않고, 해결 방법을 지어내지 않습니다(특히 매뉴얼에 적힌 일반적인 대처법을 이 상황의 원인인 것처럼 안내하지 않습니다).
6. 추측 금지: 도구 결과에 없는 사실을 만들어내지 않습니다. 모르면 모른다고 답합니다.
7. 파일 삭제·수정, 프로세스 종료 같은 파괴적 동작은 하지 않습니다.
8. user_id 등 호출자 신원 파라미터를 스스로 만들지 않습니다(시스템이 본인으로 고정합니다).

# 요청 처리

## "확인해 달라" — 실행이 필요한 요청
예: "내 홈스토리지 용량", "내 할당량", "내 작업 목록", "이 서버 GPU 상태"

1) 어떤 커맨드로 하는지 찾습니다. 한 곳에서 못 찾으면 다른 곳도 봅니다(중간 보고 없이).
   - command.search_commands (커맨드 카탈로그)
   - manual.search_manual (매뉴얼 문서에 적힌 커맨드)
   - System MCP 도구 목록 (그 작업 전용 도구가 있으면 그것을 씁니다)
2) 실행합니다.
   - System MCP에 전용 도구가 있으면 그 도구를 호출합니다.
   - 그 외에는 찾은 커맨드를 command.run_command(command="...")에 그대로 넘깁니다. 카탈로그 등록 여부와 무관하게 실행되며, "실행할까요?"라고 묻지 않습니다.
   - 인자는 args에 한 칸씩 나눠 넣습니다. host는 지정하지 않습니다(로그인 서버에서 실행). 사용자가 특정 서버를 지목한 경우에만 host를 넣습니다.
3) 실행 결과만 답합니다.
   - 개인 계정의 할당량/사용량은 df 같은 서버 전체 디스크 도구로 답하지 않습니다. 전용 커맨드를 찾아 실행합니다.
   - 세 곳 어디에도 없으면 커맨드를 지어내지 말고, 확인할 수 있는 커맨드가 없다고 답합니다.

## 사용법·절차·정책을 묻는 요청
- manual.search_manual로 찾아 답합니다(내용이 더 필요하면 manual.get_document).
- 과거 장애/문의 사례는 voc.search_voc.
- 검색 결과가 질문과 어긋나면(예: GPU를 물었는데 CPU 문서가 나옴) 그 내용을 그대로 옮기지 말고, 질문 표현을 바꿔 다시 검색합니다(최대 2회). 그래도 맞는 내용이 없으면 "매뉴얼에서 확인되지 않는다"고 답합니다.

## 서버 점검 도구 (System MCP)
도구는 관리자가 수시로 추가/변경하므로 이름을 외우지 말고, 그 도구에 host 파라미터가 있는지로 판단합니다.
- host 파라미터가 없는 도구: 로그인 서버로 고정 실행됩니다. 서버를 묻지 말고 바로 호출합니다.
- host 파라미터가 있는 도구: 사용자가 서버 이름을 밝혔으면 그 이름을, 특정 서버에 매인 질문이 아니면 로그인 서버 이름(맨 끝에 안내됨)을 넣습니다. 특정 서버 이야기인데 이름을 모르면 되묻습니다.

## 본인 스케줄러 job
- "내 job 상태" → command.get_scheduler_job_info (대상은 본인으로 자동 고정)

# 답변 형식
- 짧고 사실만. 서론·맺음말·진행 상황 설명 없이 바로 본론.
- 명령어와 실행 출력은 코드 블록으로 보여줍니다.
- 절차 안내가 필요할 때만 번호 목록을 씁니다."""


async def ensure_databases():
    """존재하지 않는 DB를 만든다(볼륨이 이미 있어 init-db가 실행되지 않은 경우 대비)."""
    conn = await asyncpg.connect(dsn("postgres"))
    try:
        for db in APP_DBS:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db}"')
                print(f"[migrate] created database {db}")
    finally:
        await conn.close()


async def apply_migrations():
    by_db: dict[str, list[tuple[int, str]]] = {}
    for db, version, sql in MIGRATIONS:
        by_db.setdefault(db, []).append((version, sql))

    for db, items in by_db.items():
        conn = await asyncpg.connect(dsn(db))
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for version, sql in sorted(items):
                if version in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                print(f"[migrate] {db}: applied v{version}")
        finally:
            await conn.close()


async def seed_config():
    conn = await asyncpg.connect(dsn("platform_config"))
    try:
        for key, value, desc, hot, secret, force in config_seed():
            if force:
                # 환경변수 기반 값: 항상 최신으로 갱신 (비밀번호 변경 자동 반영)
                await conn.execute("""
                    INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by, updated_at)
                    VALUES ($1,$2,$3,$4,$5,'bootstrap', now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, description = EXCLUDED.description,
                        hot_reload = EXCLUDED.hot_reload, is_secret = EXCLUDED.is_secret,
                        updated_by = 'bootstrap', updated_at = now()
                """, key, value, desc, hot, secret)
            else:
                # 운영자가 콘솔에서 바꿀 수 있는 값: 없을 때만 삽입(덮어쓰지 않음)
                await conn.execute("""
                    INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
                    VALUES ($1,$2,$3,$4,$5,'bootstrap')
                    ON CONFLICT (key) DO UPDATE
                    SET description = EXCLUDED.description,
                        hot_reload = EXCLUDED.hot_reload,
                        is_secret = EXCLUDED.is_secret
                """, key, value, desc, hot, secret)
        print("[migrate] config seeded")
    finally:
        await conn.close()


async def main():
    await ensure_databases()
    await apply_migrations()
    await seed_config()
    print("[migrate] done")


if __name__ == "__main__":
    asyncio.run(main())
