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
    embedding      vector(1024)
    -- 발행 여부는 개별 청크가 아니라 manual_files.status로 판단한다
);

CREATE INDEX IF NOT EXISTS manual_chunks_embedding_idx
    ON manual_chunks USING hnsw (embedding vector_cosine_ops);
