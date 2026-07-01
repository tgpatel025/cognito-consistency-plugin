-- Schema for the Cognito Consistency Platform demo.
--
-- Three tables:
--   app_users        - the "application side" mirror of Cognito identities
--   sync_audit_log    - append-only record of every sync attempt (compliance trail)
--   sync_dead_letters - failed events awaiting replay

CREATE TABLE IF NOT EXISTS app_users (
    id              SERIAL PRIMARY KEY,
    cognito_sub     TEXT UNIQUE NOT NULL,
    email           TEXT,
    username        TEXT,
    attributes      JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_users_email ON app_users (email);

CREATE TABLE IF NOT EXISTS sync_audit_log (
    id            SERIAL PRIMARY KEY,
    cognito_sub   TEXT NOT NULL,
    event_source  TEXT NOT NULL,        -- e.g. 'post_confirmation', 'post_authentication', 'reconciler'
    status        TEXT NOT NULL,        -- 'success' | 'failure'
    detail        TEXT,                 -- e.g. 'insert', 'update', or an error summary
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sync_audit_log_sub ON sync_audit_log (cognito_sub);
CREATE INDEX IF NOT EXISTS idx_sync_audit_log_occurred_at ON sync_audit_log (occurred_at);

CREATE TABLE IF NOT EXISTS sync_dead_letters (
    id            SERIAL PRIMARY KEY,
    cognito_sub   TEXT NOT NULL,
    payload       JSONB NOT NULL,
    error         TEXT NOT NULL,          -- original error from the Lambda handler
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed      BOOLEAN NOT NULL DEFAULT false,
    replayed_at   TIMESTAMPTZ,
    retry_count   INT NOT NULL DEFAULT 0, -- incremented on every failed replay attempt
    last_error    TEXT,                    -- most recent replay failure, if retry_count > 0
    last_attempted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_unreplayed ON sync_dead_letters (replayed) WHERE replayed = false;
CREATE INDEX IF NOT EXISTS idx_dead_letters_stuck ON sync_dead_letters (retry_count) WHERE replayed = false AND retry_count > 0;
