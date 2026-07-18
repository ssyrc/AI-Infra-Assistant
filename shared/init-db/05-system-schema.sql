\c system_db

CREATE TABLE IF NOT EXISTS system_whitelist_state (
    tool_name    TEXT PRIMARY KEY,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_logs (
    id           SERIAL PRIMARY KEY,
    tool_name    TEXT NOT NULL,
    params       JSONB,
    requested_by TEXT,
    status       TEXT,
    result       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
