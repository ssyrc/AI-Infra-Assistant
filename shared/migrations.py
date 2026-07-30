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
import sys
import json
import asyncio
import asyncpg

from execution_exec import DEFAULT_DENY_CSV, tool_name_for

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
    # v3: 로그인 서버를 '이름'에서 'IP'로 강제 교체.
    #     배포 호스트 /etc/hosts에서 login07이 게이트(202.20.185.100)가 아니라 75.11.29.7로
    #     풀리고 있었고, 그 서버엔 우리 키가 없어 커맨드 실행이 전부 인증 실패했다.
    #     이미 IP가 들어 있으면(=운영자가 의도적으로 정한 값) 건드리지 않는다.
    ("platform_config", 3, """
        UPDATE platform_settings
           SET value = '202.20.185.100', updated_at = now()
         WHERE key = 'scheduler_login_host'
           AND value !~ '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$';
    """),
    # v4: scheduler_job_command 설정 키 제거.
    #     스케줄러 커맨드를 설정값으로 둔 것 자체가 잘못이었다 - 커맨드는 카탈로그(커맨드 탭)에
    #     등록하고 에이전트가 검색해서 실행해야 한다. 설정 탭에 커맨드가 하나 더 생기면
    #     "어디를 고쳐야 반영되나"가 두 곳이 되어 오히려 헷갈린다.
    ("platform_config", 4, """
        DELETE FROM platform_settings WHERE key = 'scheduler_job_command';
    """),
    # v5: Command MCP + System MCP -> Execution MCP 통합(#111). 새 키는 seed_config가 넣는다.
    #   관리자가 바꿔 둔 값은 옮겨 준다 - 통합했다고 설정을 다시 하게 만들지 않는다.
    #   *_mcp_url은 옮기지 않는다(컨테이너 이름·포트가 바뀌었으므로 새 기본값이 맞다).
    ("platform_config", 5, """
        INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
        SELECT 'execution_tools_max', value, '등록 커맨드를 MCP 툴로 노출할 최대 개수',
               false, false, updated_by
        FROM platform_settings WHERE key = 'command_tools_max'
        ON CONFLICT (key) DO NOTHING;

        INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
        SELECT 'execution_deny_commands', value, '실행을 거부할 명령 이름(콤마 구분)',
               true, false, updated_by
        FROM platform_settings WHERE key = 'catalog_exec_deny_commands'
        ON CONFLICT (key) DO NOTHING;

        DELETE FROM platform_settings
        WHERE key IN ('command_tools_max', 'catalog_exec_deny_commands',
                      'command_mcp_url', 'system_mcp_url', 'command_db_dsn');
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
    # v7: VOC를 '업로드 묶음' 단위로도 다룰 수 있게 한다.
    #     CSV 한 개를 올리면 수천 행이 개별 레코드로 들어가는데, 콘솔에서 낱개로만 보이면
    #     "방금 올린 그 파일"을 통째로 되돌릴 방법이 없었다. batch_id로 묶어 두면
    #     묶음 목록/묶음 삭제가 가능해진다. 기존 데이터는 batch_id가 NULL(=출처 미상)이다.
    ("voc_db", 7, """
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS batch_id TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS source_file TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS uploaded_by TEXT;
        CREATE INDEX IF NOT EXISTS voc_records_batch_idx ON voc_records (batch_id);
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
    # v7: Command MCP + System MCP -> **Execution MCP 하나**로 합친다(#111).
    #   등록 커맨드는 여기 한 테이블로 모인다(구 command_catalog + system_custom_commands).
    #   물리 DB는 command_db를 그대로 쓴다 - 이미 올라간 카탈로그와 job_logs를 옮기지 않기 위함.
    #   설정 키는 execution_db_dsn으로 새로 두되 같은 DB를 가리킨다.
    #     exec_command: `head -n {lines} {path}` 같은 **자리표시자가 든 커맨드 한 줄**
    #     args: [{name,type,required,default,description,choices}] - 자리표시자의 타입 정의
    #     allow_extra_args: 에이전트가 정의된 인자 외에 자유 인자를 덧붙일 수 있는가
    #     host_mode: login_server(로그인 서버 고정) | target_server(LLM이 서버를 지정)
    ("command_db", 7, """
        CREATE TABLE IF NOT EXISTS execution_commands (
            id SERIAL PRIMARY KEY,
            tool_name        TEXT UNIQUE NOT NULL,
            title            TEXT NOT NULL,
            description      TEXT NOT NULL DEFAULT '',
            exec_command     TEXT NOT NULL,
            args             JSONB NOT NULL DEFAULT '[]',
            allow_extra_args BOOLEAN NOT NULL DEFAULT true,
            host_mode        TEXT NOT NULL DEFAULT 'login_server',
            enabled          BOOLEAN NOT NULL DEFAULT true,
            required_roles   TEXT[] NOT NULL DEFAULT '{}',
            updated_by       TEXT,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT execution_commands_host_mode_check
                CHECK (host_mode IN ('login_server', 'target_server'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS execution_commands_title_idx
            ON execution_commands (title);
        -- 코드 내장 커맨드(builtin.py)의 활성/역할/설명/실행위치. 구 system_whitelist_state.
        CREATE TABLE IF NOT EXISTS execution_builtin_state (
            tool_name            TEXT PRIMARY KEY,
            enabled              BOOLEAN NOT NULL DEFAULT true,
            required_roles       TEXT[],
            description_override TEXT,
            host_mode            TEXT NOT NULL DEFAULT 'target_server',
            updated_by           TEXT,
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT execution_builtin_host_mode_check
                CHECK (host_mode IN ('login_server', 'target_server'))
        );
    """),
    # v8: 커맨드 RAG 검색은 #105에서 없앴다(툴로 노출한다). 임베딩 컬럼은 그때부터 아무도 읽지
    #     않는데 업로드할 때마다 수천 건을 임베딩하느라 몇 분씩 걸리고 있었다. 통합하며 정리한다.
    ("command_db", 8, """
        DROP INDEX IF EXISTS command_catalog_embedding_idx;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embedding;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embed_model;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embed_dim;
    """),
    # reference_path = 이 문서가 실제로 있는 위치(사내 포탈 경로 등).
    #   에이전트가 "OO 문서를 참고하세요"라고만 하면 사용자는 그 문서를 찾을 수 없다.
    #   관리자가 여기에 전체 경로를 넣어 두면 답변에 경로가 그대로 붙는다.
    #   예: 슈퍼컴 Portal (https://…) > USEFUL INFO. > 활용 가이드 > GPU 서버 활용 가이드
    ("manual_db", 7, """
        ALTER TABLE manual_files ADD COLUMN IF NOT EXISTS reference_path TEXT;
    """),
    # doc_title = 이 청크가 나온 '원본 문서' 이름(PPT 파일명 등).
    #   매뉴얼 한 건에 여러 가이드 문서가 섞여 올라오는 경우가 있다. 예를 들어 '활용 가이드'
    #   메뉴 하나를 등록하면 그 안에 GPU 서버 활용 가이드, 계정 신청 가이드 …가 다 들어 있다.
    #   manual_files.title은 메뉴 이름("활용 가이드")이고, 개별 문서 이름은 행마다 다르므로
    #   청크 단위로 들고 있어야 한다. 답변에서 문서 위치를 안내할 때
    #   reference_path(메뉴까지) + doc_title(문서 이름)로 전체 경로가 완성된다.
    #   검색에도 쓰이도록 tsv에 포함한다("GPU 서버 활용 가이드" 같은 질의가 제목으로 잡힌다).
    ("manual_db", 8, """
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS doc_title TEXT;
        ALTER TABLE manual_chunks DROP COLUMN IF EXISTS tsv;
        ALTER TABLE manual_chunks ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(doc_title, '') || ' ' ||
                    coalesce(section_title, '') || ' ' || coalesce(chunk_text, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
        CREATE INDEX IF NOT EXISTS manual_chunks_doc_title_idx ON manual_chunks (doc_title);
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
        # 3-gram 축은 word_similarity(질의, 본문) 기준이라 문서 길이에 휘둘리지 않는다.
        # 올리면 정확도↑ 재현율↓. 0.2~0.4 사이가 무난하다.
        ("trgm_min_similarity", "0.3",
         "3-gram 검색축에서 후보로 받을 최소 word_similarity(0~1)", True, False, False),
        # 절차 문서에서 중간 단계가 빠지지 않도록 검색된 청크의 앞뒤를 함께 읽어 준다.
        # 0이면 확장하지 않음. 크게 잡으면 컨텍스트가 길어져 답변이 산만해진다(최대 3).
        ("manual_neighbor_window", "1",
         "매뉴얼 검색 결과에 함께 붙일 앞뒤 청크 수(0이면 붙이지 않음)", True, False, False),
        # 임베딩 모델 한도(bge-m3 8192토큰)를 넘기면 서버가 400을 돌려준다. 넘는 입력은 잘라서
        # 보낸다. 0이면 자르지 않음(모델 한도가 더 큰 경우에만).
        ("embed_max_chars", "4000",
         "임베딩에 보낼 최대 글자 수(초과분은 잘림, 0이면 자르지 않음)", True, False, False),
        # VOC 한 건이 수십만 자인 경우가 있어, 검색 결과로 넘길 때 앞부분만 자른다
        # (원문은 DB에 그대로 남는다). 0이면 자르지 않음.
        # LLM 컨텍스트(32768토큰)를 넘기지 않도록 도구 결과와 대화 이력에 상한을 둔다.
        # 실제로 매뉴얼+VOC 결과가 길어 ContextWindowExceededError(33k~35k 토큰)가 났다.
        ("voc_result_max_chars", "1500",
         "VOC 검색 결과에서 질문/답변 하나당 넘길 최대 글자 수(0이면 자르지 않음)", True, False, False),
        ("manual_result_max_chars", "1500",
         "매뉴얼 검색 결과 하나당 넘길 최대 글자 수(이웃 청크 포함, 0이면 자르지 않음)",
         True, False, False),
        ("history_max_chars", "8000",
         "에이전트에 넘길 대화 이력의 최대 글자 수(넘으면 오래된 턴부터 버림)", True, False, False),
        ("embed_cache_ttl_seconds", "86400", "쿼리 임베딩 캐시 TTL(초)", True, False, False),
        ("clean_policy_version", "1", "정제 정책 버전(캐시 키에 포함)", True, False, False),
        ("search_max_top_k", "20", "검색 top_k 상한", True, False, False),
        ("search_max_candidates", "100", "리랭킹 후보 상한", True, False, False),
        ("upload_max_mb", "50", "업로드 최대 크기(MB)", True, False, False),
        ("upload_session_ttl_minutes", "60", "업로드 미리보기 세션 유효시간(분)", True, False, False),
        ("upload_source_dir", "/data/uploads",
         "매뉴얼/VOC/커맨드 카탈로그 '서버 파일에서 선택' 목록 경로(admin-console 컨테이너 내부 "
         "경로, docker-compose에서 마운트된 폴더 하위만 가능)", True, False, False),
        # 반드시 **IP**로 둔다. 이름(login07 등)은 배포 호스트 /etc/hosts에 의존하는데,
        # 실제로 login07이 게이트 서버가 아닌 75.11.29.7로 풀려 모든 실행이 인증 실패했다.
        # 등록 커맨드를 MCP 툴로 노출하는 개수 상한. 툴 설명이 전부 프롬프트에 실리므로
        # 무한정 늘릴 수 없다. 넘치면 남는 커맨드는 run_command로만 실행 가능하다.
        ("execution_tools_max", "80",
         "등록 커맨드를 MCP 툴로 노출할 최대 개수(툴 하나당 약 100토큰이 매 요청에 실린다)",
         False, False, False),
        ("scheduler_login_host", os.environ.get("SCHEDULER_LOGIN_HOST", "202.20.185.100"),
         "커맨드를 실행할 로그인 서버 주소. 이름 말고 **IP**로 적는다(이름 해석 사고 방지)",
         True, False, False),
        # 차단 목록. 등록 커맨드의 '추가 인자'와 미등록 커맨드(run_command)의 **모든 토큰**을
        # 이 목록으로 검사한다 - `mpirun -n 4 rm -rf /`처럼 인자를 실행하는 커맨드가 있기 때문이다.
        # 콤마 구분, 비우면 제한 없음.
        ("execution_deny_commands", DEFAULT_DENY_CSV,
         "실행을 거부할 명령 이름(콤마 구분). 커맨드의 모든 토큰을 검사한다. 비우면 제한 없음",
         True, False, False),

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
        # Execution MCP 전용 DB. 물리적으로는 기존 command_db를 그대로 쓴다(#111에서 통합할 때
        # 이미 올라간 카탈로그와 job_logs를 옮기지 않기 위해 이름만 바꿨다).
        ("execution_db_dsn", dsn("command_db"), "Execution MCP 전용 DB", False, True, True),
        # 구 System MCP DB. 이관이 끝나면 읽지 않지만, 되돌릴 수 있게 남겨 둔다.
        ("system_db_dsn", dsn("system_db"), "구 System MCP DB(통합 이관용, 읽기 전용)",
         False, True, True),
        ("agent_session_db_dsn",
         dsn("agent_sessions_db").replace("postgresql://", "postgresql+asyncpg://"),
         "ADK DatabaseSessionService용 DB (asyncpg 스킴)", False, True, True),
        ("memory_db_dsn", dsn("memory_db"), "사용자별 장기 메모리 DB", False, True, True),
        ("redis_url", redis_url(), "임베딩 캐시용 Redis(비우면 캐시 미사용)", False, True, True),

        ("manual_mcp_url", os.environ.get("MANUAL_MCP_URL", "http://manual-mcp:8001/mcp"),
         "Agent Server가 연결할 Manual MCP 주소", False, False, False),
        ("execution_mcp_url", os.environ.get("EXECUTION_MCP_URL", "http://execution-mcp:8002/mcp"),
         "Agent Server가 연결할 Execution MCP 주소(커맨드 실행 전담)", False, False, False),
        ("voc_mcp_url", os.environ.get("VOC_MCP_URL", "http://voc-mcp:8003/mcp"),
         "Agent Server가 연결할 VOC MCP 주소", False, False, False),
        ("chart_mcp_url", os.environ.get("CHART_MCP_URL", "http://chart-mcp:8005/mcp"),
         "Agent Server가 연결할 Chart MCP 주소(비우면 차트 기능 없이 동작)", False, False, False),
        # **비워 두는 것이 기본이고 권장값이다.** 비어 있으면 차트를 답변 안에 그대로 박아
        # 보내므로(data URI) 설정도 열어 둘 포트도 필요 없다 - 폐쇄망에서 그대로 동작한다.
        # 이미지를 URL로 두고 싶을 때만(브라우저 캐시를 쓰거나 답변을 가볍게 하려면)
        # 배포 호스트 주소를 넣는다(예: http://202.20.183.30:8509 - 사내 주소다).
        ("chart_public_base_url", os.environ.get("CHART_PUBLIC_BASE_URL", ""),
         "(선택) 차트를 URL로 제공할 때의 사내 주소. 비우면 답변에 이미지를 직접 넣는다",
         True, False, False),
        ("chart_max_points", "200", "차트 하나에 넣을 수 있는 최대 항목 수", True, False, False),
        ("chart_retention_hours", "72",
         "생성된 차트 파일 보관 시간(시간). 지나면 자동 삭제, 0이면 삭제하지 않음", True, False, False),

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
  답을 다 쓴 뒤 **문장 하나하나가 조회 결과에 있는지 확인하고, 없는 문장은 지웁니다.**
  반대로, 조회 결과에 **있는** 내용이면 기술적으로 상세하더라도(컴파일 옵션, 파라미터 값 등)
  그대로 전달합니다 — 문서에 적혀 있다는 이유로 충분한 것이지, 스스로 걸러내지 마세요.
- 조회 결과에 없으면 "매뉴얼에서 확인되지 않습니다"라고 답합니다. 일부만 확인됐으면 확인된
  부분만 답하고 나머지는 확인되지 않았다고 밝힙니다.
- **조건을 떼고 옮기지 않습니다.** 문서의 문장에 조건·범위·예외를 나타내는 말이 붙어 있으면
  (only, ~하는 경우에만, 필요 시, 권장, 해당하는 사용자만 …) **그 말을 반드시 함께** 옮깁니다.
  조건부 안내를 필수 절차처럼 바꿔 쓰면 하지 않아도 될 일을 시키는 잘못된 답이 됩니다.
    · 문서: "Only for simulation workloads requiring CUDA compilation, you will need to
      recompile …"
    · 옳게: "CUDA 컴파일이 필요한 시뮬레이션 작업을 하는 경우에만 재컴파일이 필요합니다"
    · 틀리게: "CUDA 컴파일 시 재컴파일 필요" / 조건 없이 항목처럼 나열하는 것
- **문서에 적힌 값을 다른 값으로 바꾸거나 일반화하지 않습니다.** 플래그·숫자·경로·모델명은
  문서에 있는 그대로 씁니다. 문서에 한 가지 경우만 적혀 있으면 다른 경우를 유추해서
  만들어 내지 않습니다(예: 문서에 `compute_80`만 있는데 `compute_90`을 지어내지 않습니다).
  문서가 다루지 않는 경우는 "문서에는 …만 나와 있습니다"라고 밝힙니다.

## (B) 일반 지식 — 아는 대로 답해도 됩니다
표준 리눅스 명령어 사용법(ls, grep, tar, awk 등), 셸/Python 같은 프로그래밍 문법, 에러 메시지
해석, 일반 개념 설명.
- 도구 조회 없이 답해도 됩니다. 예시 코드도 써도 됩니다.
- 다만 **여기에 우리 인프라 이야기를 섞지 않습니다.** 서버 이름·경로·큐 이름·사내 커맨드를
  추측해서 넣지 마세요. 그게 필요해지는 순간 (A)이므로 먼저 조회합니다.
- **(B)는 "어떻게 하느냐"를 물었을 때만입니다.** 사용자가 자기 자원의 상태·내용을
  **보여 달라/확인해 달라**고 하면 (표준 리눅스 명령으로 되는 일이라도) 설명으로 끝내지 말고
  커맨드 실행 도구로 **실제로 실행해서 결과를 답합니다.**
    · "내 홈 파일 리스트 보여줘" → `ls -l` 설명이 아니라 실행 결과
    · "내 홈에 파일 목록 보는 법 알려줘" → 설명
  판단이 애매하면 실행합니다(읽기 전용이라 안전하고, 사용자가 원한 건 대개 결과입니다).

## (C) 한 질문에 (A)와 (B)가 섞여 있으면 — 부분별로 나눠 답합니다
예: "GPU 노드 접근해서 내 파일 리스트 보는 방법 알려줘"
→ "GPU 노드 접근"은 (A)라 매뉴얼을 조회해 그 내용대로, "파일 리스트 보기"는 (B)라 일반 명령
   (`ls -l` 등)으로 답합니다. 두 부분을 순서대로 이어서 하나의 답으로 만듭니다.
- (A) 부분을 먼저 조회합니다. 조회 결과에 없으면 **그 부분만** "매뉴얼에서 확인되지 않습니다"라고
  하고, (B) 부분은 정상적으로 답합니다(전체를 못 답한다고 하지 마세요).
- (B) 부분을 답할 때도 사내 고유값(홈 경로, 서버 이름, 큐 이름, 사내 커맨드)을 지어내지 않습니다.
  그게 필요하면 그건 (A)이므로 조회하거나, 확인되지 않았다고 밝힙니다.
- 뒷부분이 '방법'이 아니라 '결과 요청'이면 설명 대신 실행합니다(위 (B)의 실행 규칙).

애매하면 (A)로 보고 조회부터 합니다.

# 2. 답변 방식
- **진행 상황을 중계하지 않습니다.** "확인해 드리겠습니다", "검색해 보겠습니다", "실행하겠습니다"
  같은 문장을 쓰지 않습니다. 도구는 조용히 호출하고 **최종 결과만** 답합니다.
- 출처 꼬리말("출처: ...")을 붙이지 않습니다.
- 실행 결과는 결과 자체만 간결히 보여줍니다. 필요하면 한두 줄로 요약합니다.
- **실행 결과를 지어내지 않습니다.** 커맨드 실행 도구를 호출하지 않았다면 출력은 존재하지
  않습니다. 그럴듯한 예시 출력(파일 목록, 용량 수치, job 목록 등)을 만들어 보여주는 것은
  거짓 정보입니다. 실행하지 못했으면 **실행하지 못했다고** 말합니다.
  화면에 붙일 출력은 반드시 도구가 돌려준 것이어야 합니다.
- 커맨드가 실패하면 **실패 사실과 오류 메시지만** 짧게 전합니다. 원인을 추측하거나 해결 방법을
  지어내지 않습니다(문서에 적힌 일반 대처법을 이 상황의 원인인 것처럼 안내하지 않습니다).
- 짧고 사실만. 서론·맺음말 없이 바로 본론. 명령어와 실행 출력은 코드 블록으로.
- 파일 삭제·수정, 프로세스 종료 같은 파괴적 동작은 하지 않습니다.
- 호출자 신원(user_id 등)을 스스로 만들지 않습니다. 시스템이 본인으로 고정합니다.

# 3. 도구 사용
도구는 관리자가 콘솔에서 수시로 추가/변경합니다. **도구 이름을 외워서 쓰지 말고, 그때그때
사용 가능한 도구 목록과 각 도구의 설명을 보고 고릅니다.**

## 질문이 바뀌면 처음부터 다시 검색합니다
앞선 질문의 답을 이어 쓰지 않습니다. 새 질문이 오면 **그 질문만으로 다시 검색**하고,
이번에 조회한 내용만으로 답을 만듭니다.
- 대상만 바뀐 질문일수록 위험합니다("GPU 사용법" 다음의 "CPU 사용법"). 앞 답변의 문장·수치·
  옵션을 그대로 옮기지 말고, **CPU로 다시 검색해서 나온 내용만** 씁니다.
- 이번 검색 결과에 없는 내용이 앞 답변에 있었다면, 그건 이 질문의 근거가 아닙니다. 버립니다.
- 대화 맥락은 "무엇을 묻는지 이해하는 데"만 씁니다. **근거로는 쓰지 않습니다.**

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
1) 매뉴얼 검색 도구로 조회합니다. 커맨드가 궁금한 질문이면 **사용 가능한 커맨드 툴 목록**도
   함께 봅니다(등록된 사내 커맨드는 각각 전용 툴로 나와 있습니다).
2) 위 "검색 결과는 쓰기 전에 검증합니다"를 그대로 적용합니다(어긋나면 버리고 재검색, 최대 2회).
3) 그래도 맞는 내용이 없으면 **모른다고 답합니다.** 여기서 기억으로 채우면 안 됩니다.
4) 검색 결과의 본문에는 앞뒤 문단이 함께 붙어 옵니다. 절차를 안내할 때는 그 범위를 끝까지 읽고
   **번호가 이어지는 단계를 건너뛰지 않습니다**(1→2→4처럼 중간이 비면 안 됩니다).

## "그 문서를 보라"고 안내할 때 — **존재하는 문서만** 안내합니다
**문서 이름을 지어내지 않습니다.** "OO 관련 가이드를 참고하세요"라고 쓰려면, 먼저 매뉴얼을
검색해서 **실제로 그 문서가 검색 결과에 있어야** 합니다. 그리고 결과에 있는
doc_title(문서 이름)과 reference(전체 경로)를 **그대로** 옮겨 적습니다.
- 매뉴얼 검색을 하지 않았거나 결과에 없으면 **문서 안내 문장 자체를 쓰지 않습니다.**
  "관련 가이드를 참고하시기 바랍니다" 같은 막연한 마무리는 붙이지 마세요 —
  있지도 않은 문서를 가리키는 거짓 안내가 됩니다.
- reference는 이미 완성된 문자열입니다. 조각을 직접 이어 붙이거나 순서를 바꾸지 마세요.
- reference가 비어 있으면 경로를 지어내지 말고 doc_title(문서 이름)만 말합니다.
- 예: "GPU 서버 활용 가이드 문서를 참고하세요"(X — 경로 없음)
      "슈퍼컴 Portal > USEFUL INFO. > 활용 가이드 > GPU 서버 활용 가이드 를 참고하세요"(O)
- 과거 사례(VOC)로 답한 뒤에도 마찬가지입니다. VOC 답변 끝에 문서를 덧붙이고 싶으면
  **그때 매뉴얼을 검색해서** 실제 문서를 찾은 경우에만 씁니다.
- 답변 끝에 **참고 문서 목록**을 붙일 때도 이름만 나열하지 마세요. 한 줄에 하나씩,
  그 문서의 **reference를 그대로** 적습니다. reference가 있는데 이름만 쓰면 안 됩니다.
    · "GPU 서버 활용 가이드"(X)
    · "슈퍼컴 Portal > USEFUL INFO. > 활용 가이드 > GPU 서버 활용 가이드"(O)

## 여러 가이드 문서가 함께 검색될 수 있습니다
한 메뉴에 여러 문서가 들어 있어서, 결과마다 doc_title(원본 문서 이름)이 다를 수 있습니다.
- **doc_title이 다른 내용을 하나의 절차처럼 이어 붙이지 않습니다.** 계정 신청 가이드의 3단계
  뒤에 GPU 가이드의 4단계를 붙이면 존재하지 않는 절차가 됩니다.
- 한 답변에서 여러 문서를 인용해야 하면 **문서별로 나눠서** 쓰고, 각각의 reference를 답니다.
- 질문이 특정 문서에 대한 것이면 그 doc_title의 결과만 씁니다.

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

## 개인·조직 정보는 그대로 쓰지 않습니다 — **검색된 문서에만 적용됩니다**
매뉴얼·과거 사례(VOC)처럼 **다른 사람의 기록을 검색해 온 것**에 남의 계정·이름·조직이 있으면
자리표시자로 바꿔서 답합니다.
- 계정·사번·이메일 → {사용자 id} · 사람 이름(한글/영문/외국 이름 모두) → {사용자 이름}
- 조직명 → {사업부명} {센터명} {팀명} {그룹명} {파트명} 등 단위에 맞는 자리표시자
- 조회 결과에 이미 자리표시자가 들어 있으면 **그대로 두고**, 실제 값을 추측해 채우지 않습니다.
  `{부문명}`을 "DS부문"으로, `{팀명}`을 실제 팀 이름으로 바꿔 쓰면 안 됩니다 — 학습 지식으로
  메운 값이라 틀릴 뿐 아니라, 가리려고 지운 정보를 되살리는 것이 됩니다.

**커맨드 실행 결과는 절대 가리지 않습니다.** 실행은 언제나 질문한 본인 계정 권한으로만
이뤄지므로 거기 나오는 계정명·경로·파일명은 전부 본인 정보입니다. 출력을 **원문 그대로**
보여주세요.
- `ls -l` 결과의 소유자 계정을 {사용자 id}로 바꾸면 안 됩니다 — 자기 파일 목록을 못 알아봅니다.
- 홈 경로, 쿼터 조회의 계정명, job 목록의 사용자명도 마찬가지로 그대로 둡니다.

## 커맨드는 **한 번에** 실행합니다 — 탐색하지 마세요
커맨드 실행은 매번 원격 접속이라 한 번에 몇 초씩 듭니다. 같은 답을 얻으려고 여러 번
돌리면 사용자는 그만큼 기다립니다.
- **경로를 알아내려고 미리 돌리지 않습니다.** `echo $HOME`, `pwd`, `whoami` 같은 확인용
  커맨드를 앞세우지 마세요. 실행은 **항상 본인 계정의 홈에서 시작**하므로,
  홈을 보려면 경로를 비우거나 `.`을 쓰면 됩니다.
- **같은 도구를 같은 인자로 두 번 부르지 않습니다.** 결과가 비어 있어도 그건 "비어 있다"는
  답이지 오류가 아닙니다. 그대로 답하세요.
- 필요한 커맨드가 여럿이면 각각 한 번씩만 부르고, 결과를 모아 한 번에 답합니다.

## "확인해 달라" — 실행이 필요한 요청
예: "내 홈스토리지 용량", "내 job 목록", "내 작업 상태", "이 서버 GPU 상태"
0) 커맨드 툴은 전부 **로그인 서버에서 호출자 본인 권한으로** 실행되며, 추가 인자가 필요하면
   `args`에 한 칸씩 나눠 넣습니다(예: `["-l", "/home"]`). 툴 설명의 `[...]` 안이 실제 커맨드입니다.
1) **먼저 사용 가능한 툴 목록을 봅니다.** 등록된 사내 커맨드는 각각 전용 툴로 나와 있고,
   툴 설명에 무엇을 하는지와 실제 실행되는 커맨드가 적혀 있습니다. 맞는 툴이 있으면 그걸 씁니다
   (스케줄러 job 조회, 스토리지 용량 조회 등도 전부 여기 해당합니다).
   `phd info` 같은 커맨드를 기억으로 지어내지 말고, **툴 목록에 있는 것**을 씁니다.
2) 툴 목록에 없으면 매뉴얼을 검색합니다. 매뉴얼 본문에서 찾은 커맨드는 등록 여부와 무관하게
   커맨드 실행 도구(run_command)로 그대로 실행합니다.
3) 실행할 때 "실행할까요?"라고 묻지 않습니다. 추가 인자는 인자 목록에 한 칸씩 나눠 넣습니다.
4) 실행 결과만 답합니다. 개인 계정의 할당량/사용량을 서버 전체 디스크 도구(df 등)로 답하지 않습니다.
5) **"커맨드가 없다"고 답하기 전에 반드시 매뉴얼 검색을 합니다.** 툴 목록에 없어도 매뉴얼
   본문에 커맨드가 적혀 있는 경우가 많습니다. 툴 목록 확인 → **매뉴얼 검색**(표현을 바꿔 한 번 더)
   → 그래도 없을 때만 "확인할 수 있는 커맨드가 없습니다"라고 답합니다.

## 서버 점검 도구를 쓸 때
대상 서버(host) 파라미터가 있는지로 판단합니다.
- 없으면: 로그인 서버로 고정 실행됩니다. 서버를 묻지 말고 바로 호출합니다.
- 있으면: 사용자가 밝힌 서버 이름을, 특정 서버에 매인 질문이 아니면 로그인 서버 이름(맨 끝에
  안내됨)을 넣습니다. 특정 서버 이야기인데 이름을 모르면 되묻습니다.

## 커맨드를 실행하지 못했다고 나오면
파괴적이거나 다른 명령을 대신 실행할 수 있는 커맨드는 시스템이 거부합니다
(`rm`, `chmod`, `bash -c`, `docker`, `ssh` 등. 다른 커맨드의 인자에 섞여 있어도 거부됩니다).
- 거부되면 **우회하지 않습니다.** 다른 이름으로 바꾸거나 경로를 붙여 다시 시도하지 마세요.
- 거부 사유를 그대로 전하고, 필요하면 관리자에게 커맨드 등록을 요청하도록 안내합니다.

## 그래프로 보여 달라고 하면
"추이/그래프/차트로 보여줘"라고 하거나, 커맨드 실행 결과에 기간·항목별 수치가 여럿 있어
그림이 이해에 도움이 될 때 차트 생성 도구를 씁니다.
- **숫자는 반드시 조회·실행해서 얻은 값만** 넣습니다. 그래프를 만들려고 값을 지어내지 않습니다.
  값이 없으면 차트를 만들지 말고 값이 없다고 답합니다.
- 도구가 돌려준 `markdown` 한 줄을 답변에 **그대로** 붙여 넣습니다(주소를 고치지 마세요).
  그 한 줄이 그림으로 표시됩니다.
- 그림만 남기지 말고, 무엇을 그린 것인지 한두 줄로 함께 설명합니다.
- `warning`이 함께 오면 그림이 표시되지 않는 상태입니다. 그 사유를 사용자에게 알립니다."""


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


async def import_execution_registry():
    """구 Command/System MCP의 등록 내용을 Execution MCP의 한 테이블로 옮긴다(#111).

    SQL 마이그레이션으로 못 하는 이유가 둘이다.
      1) `system_custom_commands`는 **다른 데이터베이스**(system_db)에 있다.
      2) 한글 이름에서 ASCII 툴 이름을 만드는 규칙이 파이썬 코드(registry.tool_name_for)에 있다.
    여러 번 돌려도 안전하다 - 이미 있는 title은 건너뛴다(관리자가 콘솔에서 고친 값을 덮지 않는다).
    """
    # tool_name_for는 shared에 있다. db-init 컨테이너에는 mcp_servers가 마운트되지 않으므로
    # MCP 쪽 모듈을 import하려 하면 이관이 조용히 건너뛰어진다(실제로 그렇게 실패했다).
    conn = await asyncpg.connect(dsn("command_db"))
    try:
        taken = {r["tool_name"] for r in
                 await conn.fetch("SELECT tool_name FROM execution_commands")}
        have_titles = {r["title"] for r in
                       await conn.fetch("SELECT title FROM execution_commands")}
        moved = unrunnable = 0
        # **항상** 출처와 결과 건수를 찍는다. 옮길 게 없을 때 아무 말도 하지 않으면
        # "이관이 된 건지 안 된 건지" 알 수 없다 - #112에서 실제로 그래서 헷갈렸다.
        src_catalog = await conn.fetchval("SELECT count(*) FROM command_catalog") or 0
        already = len(have_titles)

        # (1) 커맨드 카탈로그(매뉴얼 엑셀 업로드본). 인자 정의가 없고 자유 인자를 허용하던 것들이라
        #     allow_extra_args=true, 로그인 서버 고정으로 옮긴다(지금 동작과 같다).
        for r in await conn.fetch(
                "SELECT name, description, exec_command FROM command_catalog ORDER BY name"):
            title = (r["name"] or "").strip()
            if not title or title in have_titles:
                continue
            exec_command = (r["exec_command"] or "").strip() or title
            # 실행 커맨드 열이 비어 있으면 예전에는 '이름'을 그대로 실행했다. 이름이 한글이면
            # 실행될 수 없는 커맨드인데, 툴로 노출되면 프롬프트 예산만 잡아먹고 매번 실패한다.
            # 옮기기는 하되 **비활성**으로 넣어, 관리자가 콘솔에서 커맨드를 채워 켜게 한다.
            runnable = exec_command.isascii()
            if not runnable:
                unrunnable += 1
            name = tool_name_for(title, taken, exec_command)
            taken.add(name)
            have_titles.add(title)
            await conn.execute(
                """
                INSERT INTO execution_commands
                    (tool_name, title, description, exec_command, args, allow_extra_args,
                     host_mode, enabled, updated_by)
                VALUES ($1,$2,$3,$4,'[]'::jsonb, true, 'login_server', $5, 'migrate')
                ON CONFLICT (tool_name) DO NOTHING
                """,
                name, title, (r["description"] or "").strip(), exec_command, runnable)
            moved += 1

        # (2) 콘솔에서 등록한 System MCP 커스텀 커맨드(다른 DB). argv 리스트 + params를
        #     새 형식(커맨드 한 줄 + args 정의)으로 바꿔 옮긴다.
        rows = []
        try:
            sysconn = await asyncpg.connect(dsn("system_db"))
        except Exception as e:  # noqa: BLE001
            print(f"[migrate] system_db에 접속하지 못해 커스텀 커맨드 이관을 건너뜁니다: {e}")
            sysconn = None
        if sysconn is not None:
            try:
                rows = await sysconn.fetch(
                    "SELECT tool_name, description, argv_template, params, required_roles, "
                    "enabled, host_mode FROM system_custom_commands")
                states = await sysconn.fetch(
                    "SELECT tool_name, enabled, required_roles, description_override, host_mode "
                    "FROM system_whitelist_state")
            except Exception as e:  # noqa: BLE001
                print(f"[migrate] 구 System MCP 테이블을 읽지 못했습니다(무시): {e}")
                rows, states = [], []
            finally:
                await sysconn.close()

            for r in rows:
                title = (r["tool_name"] or "").strip()
                if not title or title in have_titles:
                    continue
                argv = json.loads(r["argv_template"]) if isinstance(r["argv_template"], str) \
                    else (r["argv_template"] or [])
                params = json.loads(r["params"]) if isinstance(r["params"], str) \
                    else (r["params"] or [])
                # argv 리스트 -> 한 줄. 토큰에 공백이 있으면 따옴표로 묶어 원래 경계를 지킨다.
                exec_command = " ".join(
                    (f'"{t}"' if " " in str(t) else str(t)) for t in argv)
                args = [{"name": p.get("name"), "type": p.get("type", "str"),
                         "required": True, "default": "", "description": ""} for p in params]
                taken.add(title)
                have_titles.add(title)
                await conn.execute(
                    """
                    INSERT INTO execution_commands
                        (tool_name, title, description, exec_command, args, allow_extra_args,
                         host_mode, enabled, required_roles, updated_by)
                    VALUES ($1,$2,$3,$4,$5::jsonb, false, $6, $7, $8, 'migrate')
                    ON CONFLICT (tool_name) DO NOTHING
                    """,
                    title, title, (r["description"] or "").strip(), exec_command,
                    json.dumps(args, ensure_ascii=False), r["host_mode"] or "target_server",
                    r["enabled"], list(r["required_roles"] or []))
                moved += 1

            # (3) 내장 커맨드의 on/off·역할·설명·실행위치는 관리자가 조정해 둔 값이다. 그대로 옮긴다.
            for s in states:
                await conn.execute(
                    """
                    INSERT INTO execution_builtin_state
                        (tool_name, enabled, required_roles, description_override, host_mode,
                         updated_by, updated_at)
                    VALUES ($1,$2,$3,$4,$5,'migrate', now())
                    ON CONFLICT (tool_name) DO NOTHING
                    """,
                    s["tool_name"], s["enabled"], s["required_roles"],
                    s["description_override"], s["host_mode"] or "target_server")

        src_custom = 0 if sysconn is None else len(rows)
        total_now = await conn.fetchval("SELECT count(*) FROM execution_commands") or 0
        print(f"[migrate] execution 이관: 카탈로그 {src_catalog}건 · 구 커스텀 커맨드 "
              f"{src_custom}건 · 이미 옮겨져 있던 것 {already}건 → 신규 {moved}건, "
              f"현재 등록 커맨드 총 {total_now}건")
        if src_catalog == 0 and src_custom == 0 and total_now == 0:
            print("[migrate] 옮길 커맨드가 없습니다. 구 카탈로그가 비어 있다는 뜻이니, "
                  "관리자 콘솔 실행 탭에서 직접 등록하거나 엑셀로 일괄 등록하세요.")
        if unrunnable:
            print(f"[migrate] 그중 {unrunnable}건은 실행 커맨드가 비어 있어(이름이 한글) "
                  "**비활성**으로 넣었습니다. 관리자 콘솔 실행 탭에서 실행 커맨드를 채우고 켜세요.")
    finally:
        await conn.close()


async def main():
    await ensure_databases()
    await apply_migrations()
    await import_execution_registry()
    await seed_config()
    print("[migrate] done")


if __name__ == "__main__":
    asyncio.run(main())
