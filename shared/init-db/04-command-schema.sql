\c command_db

CREATE TABLE IF NOT EXISTS command_catalog (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    description  TEXT NOT NULL,
    usage        TEXT,
    category     TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
