\c voc_db

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS voc_records (
    id           SERIAL PRIMARY KEY,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    resolved     BOOLEAN NOT NULL DEFAULT true,
    department   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding    vector(1024),
    tsv          tsvector GENERATED ALWAYS AS (
                    to_tsvector('simple', coalesce(question, '') || ' ' || coalesce(answer, ''))
                 ) STORED
);

CREATE INDEX IF NOT EXISTS voc_records_tsv_idx ON voc_records USING gin (tsv);

CREATE INDEX IF NOT EXISTS voc_records_embedding_idx
    ON voc_records USING hnsw (embedding vector_cosine_ops);
