\c manual_db

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS manual_files (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,   -- 문서 논리명 (재업로드 시 버전을 묶는 키)
    filename      TEXT NOT NULL,   -- 업로드 원본 파일명
    source_type   TEXT NOT NULL DEFAULT 'document',  -- document(docx/pptx/pdf) | spreadsheet(xlsx)
    uploaded_by   TEXT,
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ,
    version       INT NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'draft'  -- draft | published | archived
);

CREATE TABLE IF NOT EXISTS manual_chunks (
    id             SERIAL PRIMARY KEY,
    manual_file_id INT REFERENCES manual_files(id) ON DELETE CASCADE,
    seq            INT NOT NULL DEFAULT 0,   -- 문서 내 순서
    section_title  TEXT,
    page_no        INT,
    chunk_text     TEXT NOT NULL,
    embedding      vector(1024),
    -- 하이브리드 검색용 전문검색 벡터 (chunk_text로부터 자동 생성).
    -- simple 구성을 써서 언어에 독립적으로 토큰화한다(한국어 형태소 분석기가 없는 폐쇄망 대비).
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text, ''))) STORED
    -- 발행 여부는 개별 청크가 아니라 manual_files.status로 판단한다
);

CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS manual_chunks_embedding_idx
    ON manual_chunks USING hnsw (embedding vector_cosine_ops);
