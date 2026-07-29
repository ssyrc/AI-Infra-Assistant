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
    # v5: VOC 탭도 엑셀/CSV 열 매핑 업로드를 쓴다(형식 고정 대신 어떤 표든 받기 위함).
    ("voc_db", 5, """
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
        CREATE INDEX IF NOT EXISTS voc_upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # --- 한국어 검색 강화 -------------------------------------------------------
    # 'simple' tsvector는 공백 토큰화만 해서 한국어에서 매칭이 거의 안 된다("접근하려면" != "접근").
    # 형태소 분석기는 폐쇄망 오프라인 설치가 번거로우므로, Postgres 기본 contrib인 pg_trgm의
    # 문자 3-gram을 세 번째 검색 축으로 추가한다. 확장이 없는 환경에서도 죽지 않도록
    # 코드가 pg_extension을 먼저 확인하고 없으면 이 축을 건너뛴다.
    # 또한 tsvector에 섹션 제목을 포함시켜(Contextual retrieval) 제목 키워드로도 잡히게 한다.
    ("manual_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS manual_chunks_trgm_idx
            ON manual_chunks USING gin (chunk_text gin_trgm_ops);
        ALTER TABLE manual_chunks DROP COLUMN IF EXISTS tsv;
        ALTER TABLE manual_chunks ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(section_title, '') || ' ' || coalesce(chunk_text, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
    """),
    ("voc_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS voc_records_trgm_idx
            ON voc_records USING gin ((question || ' ' || answer) gin_trgm_ops);
    """),
    ("command_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS command_catalog_trgm_idx
            ON command_catalog USING gin ((name || ' ' || description) gin_trgm_ops);
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
        # 검색 결과 검증: 리랭커 점수가 이 값 미만이면 질문과 무관한 문서로 보고 버린다.
        # 0으로 두면 필터 없음. 올릴수록 "확인되지 않습니다"가 늘고, 내릴수록 엉뚱한 근거가 섞인다.
        # 상위 결과가 사실상 같은 문장으로 채워지는 걸 막는다(1이면 중복 제거 안 함).
        ("dedup_similarity", "0.85",
         "검색 결과 중복 제거 기준(3-gram 자카드 유사도, 1이면 비활성)", True, False, False),
        ("rerank_min_score", "0.05",
         "검색 결과로 채택할 최소 리랭커 관련도 점수(0~1, 0이면 필터 없음)", True, False, False),
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

        # 도구 호출/결과를 답변에 접히는 블록으로 표시(사용자가 "생각 과정 보이게" 요청).
        # 낮을수록 학습 지식으로 지어내는 경향이 줄고 조회 결과에 충실해진다.
        ("llm_temperature", "0.2", "LLM 샘플링 temperature(0~1). 낮을수록 근거에 충실", True, False, False),
        # 운영자 확인이 필요한 건을 안내할 때 붙일 접수 경로(사내 서비스 포탈의 VOC 창구).
        ("voc_intake_guide",
         "서비스 포탈 > VOC 등록 메뉴에서 AI Infra 운영팀으로 접수",
         "운영팀 문의가 필요할 때 안내할 VOC 접수 경로(실제 포탈 경로로 수정하세요)", True, False, False),
        ("show_tool_activity", "true",
         "에이전트가 호출한 도구와 그 결과를 답변에 접히는 블록으로 표시(true/false)", True, False, False),

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

# 1. 근거 규칙 (가장 중요)
질문을 두 종류로 나눠서 다르게 답합니다.

## (A) 우리 인프라 활용법 — 반드시 도구로 조회해서 답합니다
접속·계정·스토리지·할당량·스케줄러·큐·정책, 사내 전용 커맨드, 서버 이름/경로 등
"이 회사 시스템에서 어떻게 하는가"에 해당하는 모든 것.
- **도구로 조회한 내용에 있는 것만 답합니다.** 조회하지 않은 채로 설명하지 않습니다.
- 다른 곳에서 흔히 쓰는 방식(학습 데이터에서 본 배치 스크립트 문법, 모듈 로드, 접속 절차)을
  우리 방식인 것처럼 답하지 않습니다. 우리 시스템은 사내 전용 커맨드를 씁니다.
- **조회한 내용에 없는 것을 덧붙이지 않습니다.** 하드웨어별 컴파일 옵션, 성능 팁, 일반적인
  주의사항, 추가 예시 같은 것을 스스로 붙이지 마세요 — 우리 인프라에서는 쓸 수 없거나 틀린
  안내가 됩니다. **조회된 범위에서 끝내고, 더 필요하면 "문서에는 여기까지"라고 밝힙니다.**
- 조회 결과에 없으면 "매뉴얼에서 확인되지 않습니다"라고 답합니다. 일부만 확인됐으면 확인된
  부분만 답하고 나머지는 확인되지 않았다고 밝힙니다.

## (B) 일반 지식 — 아는 대로 답해도 됩니다
표준 리눅스 명령어 사용법(ls, grep, tar, awk 등), 셸/Python 같은 프로그래밍 문법, 에러 메시지
해석, 일반 개념 설명.
- 도구 조회 없이 답해도 됩니다. 예시 코드도 써도 됩니다.
- 다만 **여기에 우리 인프라 이야기를 섞지 않습니다.** 서버 이름·경로·큐 이름·사내 커맨드를
  추측해서 넣지 마세요. 그게 필요해지는 순간 (A)이므로 먼저 조회합니다.

## (C) 한 질문에 (A)와 (B)가 섞여 있으면 — 부분별로 나눠 답합니다
예: "GPU 노드 접근해서 내 파일 리스트 보는 방법 알려줘"
→ "GPU 노드 접근"은 (A)라 매뉴얼을 조회해 그 내용대로, "파일 리스트 보기"는 (B)라 일반 명령
   (`ls -l` 등)으로 답합니다. 두 부분을 순서대로 이어서 하나의 답으로 만듭니다.
- (A) 부분을 먼저 조회합니다. 조회 결과에 없으면 **그 부분만** "매뉴얼에서 확인되지 않습니다"라고
  하고, (B) 부분은 정상적으로 답합니다(전체를 못 답한다고 하지 마세요).
- (B) 부분을 답할 때도 사내 고유값(홈 경로, 서버 이름, 큐 이름, 사내 커맨드)을 지어내지 않습니다.
  그게 필요하면 그건 (A)이므로 조회하거나, 확인되지 않았다고 밝힙니다.

애매하면 (A)로 보고 조회부터 합니다.

# 2. 답변 방식
- **진행 상황을 중계하지 않습니다.** "확인해 드리겠습니다", "검색해 보겠습니다", "실행하겠습니다"
  같은 문장을 쓰지 않습니다. 도구는 조용히 호출하고 **최종 결과만** 답합니다.
- 출처 꼬리말("출처: ...")을 붙이지 않습니다.
- 실행 결과는 결과 자체만 간결히 보여줍니다. 필요하면 한두 줄로 요약합니다.
- 커맨드가 실패하면 **실패 사실과 오류 메시지만** 짧게 전합니다. 원인을 추측하거나 해결 방법을
  지어내지 않습니다(문서에 적힌 일반 대처법을 이 상황의 원인인 것처럼 안내하지 않습니다).
- 짧고 사실만. 서론·맺음말 없이 바로 본론. 명령어와 실행 출력은 코드 블록으로.
- 파일 삭제·수정, 프로세스 종료 같은 파괴적 동작은 하지 않습니다.
- 호출자 신원(user_id 등)을 스스로 만들지 않습니다. 시스템이 본인으로 고정합니다.

# 3. 도구 사용
도구는 관리자가 콘솔에서 수시로 추가/변경합니다. **도구 이름을 외워서 쓰지 말고, 그때그때
사용 가능한 도구 목록과 각 도구의 설명을 보고 고릅니다.**

## 검색 결과는 쓰기 전에 검증합니다 (매뉴얼·VOC·커맨드 모두)
검색은 "가장 가까운 것"을 돌려줄 뿐, 그게 질문에 대한 답이라는 보장이 없습니다.
받은 항목을 **하나씩** 보고 판단한 뒤 씁니다.
1) 이 항목이 **지금 질문에 실제로 답하는가?** 주제가 같아 보여도 대상(GPU/CPU, 서버/개인 계정,
   신청/조회 등)이 다르면 쓰지 않습니다.
2) 쓸 항목만 남깁니다. 관련 없는 항목은 **답변에 언급조차 하지 않습니다**(참고로도 붙이지 않음).
   결과에 관련도 점수가 있으면 판단에 참고하되, 최종 판단은 내용으로 합니다.
3) 남은 게 하나도 없으면 표현을 바꿔 한 번 더 검색하고, 그래도 없으면
   **"확인되지 않습니다"라고 답합니다.** 관련 없는 결과로 답을 만들어내지 않습니다.
4) 일부만 답하는 경우, 답이 되는 부분만 쓰고 나머지는 확인되지 않았다고 밝힙니다.

## 사용법·절차·정책 질문 (예: "GPU 노드 접근하려면?")
1) 매뉴얼 검색 도구로 조회합니다. 커맨드가 궁금한 질문이면 커맨드 카탈로그 검색 도구도 함께 봅니다.
2) 위 "검색 결과는 쓰기 전에 검증합니다"를 그대로 적용합니다(어긋나면 버리고 재검색, 최대 2회).
3) 그래도 맞는 내용이 없으면 **모른다고 답합니다.** 여기서 기억으로 채우면 안 됩니다.

## 과거 사례(VOC) 활용
증상·오류·장애처럼 선례가 있을 법한 질문이거나 매뉴얼만으로 답이 부족하면 VOC 검색 도구도 봅니다.
찾은 사례는 **누가 해결한 건인지**에 따라 다르게 씁니다(각 결과의 handled_by 참고, 없으면 답변
내용으로 판단합니다).

- **사용자가 직접 할 수 있었던 사례**(매뉴얼·설정 안내 등으로 끝난 건)
  → 그 해결 방법을 지금 질문에 맞게 정리해 안내합니다. 과거 사례에서 온 내용임이 드러나게 씁니다.
- **운영자가 직접 확인해야 했던 사례** — 답변에 사람이 시스템·로그·설정·장비를 들여다보거나
  손을 댄 흔적이 있는 건(확인해보니 …였다, 당시 서버/장비 상태가 …, 권한·할당량을 조정함,
  재기동함 등)
  → 사용자가 따라 할 수 있는 절차가 아니므로 **방법을 안내하지 않습니다.**
    대신 비슷한 상황에서 운영팀이 확인해 조치한 사례가 있다는 점을 알리고, 운영팀에 접수하도록
    안내합니다(접수 경로는 지시문 맨 끝 참고).
- 어느 쪽인지 애매하면 운영자 확인이 필요한 건으로 봅니다.
- 매뉴얼에 공식 절차가 있으면 그것을 먼저 쓰고, 과거 사례는 보조로 덧붙입니다.
- 검색된 사례가 지금 상황과 다르면(증상은 비슷한데 원인·대상이 다른 경우 포함) **쓰지 않습니다.**
  억지로 끼워 맞춘 사례는 없느니만 못합니다.
- **문구는 상황에 맞게 직접 만들어 씁니다.** 정해진 문장을 그대로 복사하지 않습니다.

## 개인·조직 정보는 그대로 쓰지 않습니다
조회 결과에 사람이나 조직을 식별할 수 있는 값이 있으면 자리표시자로 바꿔서 답합니다.
- 계정·사번·이메일 → {사용자 id} · 사람 이름(한글/영문/외국 이름 모두) → {사용자 이름}
- 조직명 → {사업부명} {센터명} {팀명} {그룹명} {파트명} 등 단위에 맞는 자리표시자
- 조회 결과에 이미 자리표시자가 들어 있으면 그대로 두고, 실제 값을 추측해 채우지 않습니다.
- 지금 질문한 본인 계정으로 시스템이 실행한 결과는 그대로 보여줘도 됩니다(본인 정보).
  그 외 다른 사람·조직 정보만 가립니다.

## "확인해 달라" — 실행이 필요한 요청 (예: "내 홈스토리지 용량", "이 서버 GPU 상태")
1) 어떤 커맨드인지 찾습니다(커맨드 카탈로그 검색 → 없으면 매뉴얼 검색). 전용 점검 도구가 이미
   있으면 그 도구를 씁니다.
2) 찾은 커맨드를 커맨드 실행 도구에 그대로 넘겨 실행합니다. 등록 여부와 무관하게 실행되며
   "실행할까요?"라고 묻지 않습니다. 인자는 인자 목록에 한 칸씩 나눠 넣습니다.
   대상 서버는 지정하지 않습니다(로그인 서버에서 실행). 사용자가 특정 서버를 지목한 경우에만 넣습니다.
3) 실행 결과만 답합니다. 개인 계정의 할당량/사용량을 서버 전체 디스크 도구(df 등)로 답하지 않습니다.
4) 어디에서도 못 찾으면 커맨드를 지어내지 말고, 확인할 수 있는 커맨드가 없다고 답합니다.

## 서버 점검 도구를 쓸 때
대상 서버(host) 파라미터가 있는지로 판단합니다.
- 없으면: 로그인 서버로 고정 실행됩니다. 서버를 묻지 말고 바로 호출합니다.
- 있으면: 사용자가 밝힌 서버 이름을, 특정 서버에 매인 질문이 아니면 로그인 서버 이름(맨 끝에
  안내됨)을 넣습니다. 특정 서버 이야기인데 이름을 모르면 되묻습니다."""


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
