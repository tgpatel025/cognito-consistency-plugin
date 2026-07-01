"""
Worked example: a UserRepository implementation against a pre-existing
schema that looks nothing like infra/localstack/schema.sql, to prove the
interface is actually usable for a different shape, not just a rename of
the same tables.

Imagined pre-existing schema (a typical "we had a users table before we
ever heard of this project" shape):

    CREATE TABLE users (
        id            SERIAL PRIMARY KEY,      -- app's own integer PK, NOT cognito_sub
        cognito_id    TEXT UNIQUE,              -- cognito_sub lives here, as a nullable column
        email_address TEXT,                      -- different column name than 'email'
        display_name  TEXT,                      -- different column name than 'username'
        metadata      JSONB DEFAULT '{}',
        updated_at    TIMESTAMPTZ DEFAULT now()
    );

    -- generic event log the app already had for other purposes,
    -- reused here instead of a dedicated sync_audit_log table
    CREATE TABLE event_log (
        id          SERIAL PRIMARY KEY,
        entity_type TEXT,
        entity_id   TEXT,
        event_type  TEXT,
        outcome     TEXT,
        notes       TEXT,
        logged_at   TIMESTAMPTZ DEFAULT now()
    );

    -- a generic failed-jobs table, reused for dead letters
    CREATE TABLE failed_jobs (
        job_id        SERIAL PRIMARY KEY,
        job_type      TEXT,
        reference_id  TEXT,
        job_data      JSONB,
        error_message TEXT,
        attempts      INT DEFAULT 0,
        resolved      BOOLEAN DEFAULT false,
        created_at    TIMESTAMPTZ DEFAULT now(),
        last_attempt_at TIMESTAMPTZ
    );

Notice this implementation:
  - keys on `cognito_id`, not `cognito_sub` -- the interface doesn't
    care what the column is called, only that upsert_user/get_all_users
    behave correctly
  - reuses generic `event_log` and `failed_jobs` tables instead of
    creating new sync-specific tables
  - maps `email_address`/`display_name` to the interface's
    `email`/`username` keys in get_all_users' return value

This file is meant to be copied and adapted, not imported directly --
your real schema will differ from this imagined one. To use it as
written against a database that happens to match this exact imagined
schema, point REPOSITORY_CLASS (see common/service_factory.py) at
"common.repositories.example_custom_schema:ExampleCustomSchemaRepository".
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from common.repositories.base import UserRepository


class ExampleCustomSchemaRepository(UserRepository):
    def __init__(self, connect_fn):
        self._connect_fn = connect_fn

    @contextmanager
    def _cursor(self, commit=True):
        conn = self._connect_fn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_user(
        self,
        cognito_sub: str,
        email: Optional[str],
        username: Optional[str],
        attributes: dict,
    ) -> dict:
        now = datetime.now(timezone.utc)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (cognito_id, email_address, display_name, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (cognito_id)
                DO UPDATE SET
                    email_address = EXCLUDED.email_address,
                    display_name  = EXCLUDED.display_name,
                    metadata      = EXCLUDED.metadata,
                    updated_at    = EXCLUDED.updated_at
                RETURNING id, (xmax = 0) AS inserted
                """,
                (cognito_sub, email, username, json.dumps(attributes), now),
            )
            row = cur.fetchone()
        return {"id": row["id"], "inserted": row["inserted"]}

    def get_all_users(self) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                "SELECT cognito_id, email_address, display_name, metadata, updated_at FROM users WHERE cognito_id IS NOT NULL"
            )
            rows = cur.fetchall()

        # Map this schema's column names to the interface's expected keys.
        return [
            {
                "cognito_sub": row["cognito_id"],
                "email": row["email_address"],
                "username": row["display_name"],
                "attributes": row["metadata"],
                "last_synced_at": row["updated_at"],
            }
            for row in rows
        ]

    def log_sync_event(
        self,
        cognito_sub: str,
        event_source: str,
        status: str,
        detail: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_log (entity_type, entity_id, event_type, outcome, notes, logged_at)
                VALUES ('user_sync', %s, %s, %s, %s, %s)
                """,
                (cognito_sub, event_source, status, detail, datetime.now(timezone.utc)),
            )

    def enqueue_dead_letter(self, cognito_sub: str, payload: dict, error: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO failed_jobs (job_type, reference_id, job_data, error_message, attempts, resolved, created_at)
                VALUES ('cognito_sync', %s, %s, %s, 0, false, %s)
                """,
                (cognito_sub, json.dumps(payload), str(error), datetime.now(timezone.utc)),
            )

    def fetch_unreplayed_dead_letters(self, max_retry: int) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT job_id, reference_id, job_data, attempts
                FROM failed_jobs
                WHERE job_type = 'cognito_sync' AND resolved = false AND attempts < %s
                ORDER BY created_at
                """,
                (max_retry,),
            )
            rows = cur.fetchall()

        return [
            {
                "id": row["job_id"],
                "cognito_sub": row["reference_id"],
                "payload": row["job_data"],
                "retry_count": row["attempts"],
            }
            for row in rows
        ]

    def fetch_stuck_dead_letters(self, max_retry: int) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT job_id, reference_id, attempts, error_message, created_at, last_attempt_at
                FROM failed_jobs
                WHERE job_type = 'cognito_sync' AND resolved = false AND attempts >= %s
                ORDER BY created_at
                """,
                (max_retry,),
            )
            rows = cur.fetchall()

        return [
            {
                "id": row["job_id"],
                "cognito_sub": row["reference_id"],
                "retry_count": row["attempts"],
                "last_error": row["error_message"],
                "occurred_at": row["created_at"],
                "last_attempted_at": row["last_attempt_at"],
            }
            for row in rows
        ]

    def mark_dead_letter_replayed(self, dead_letter_id: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE failed_jobs SET resolved = true, last_attempt_at = %s WHERE job_id = %s",
                (datetime.now(timezone.utc), dead_letter_id),
            )

    def record_dead_letter_failure(self, dead_letter_id: Any, error: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE failed_jobs
                SET attempts = attempts + 1, error_message = %s, last_attempt_at = %s
                WHERE job_id = %s
                """,
                (str(error), datetime.now(timezone.utc), dead_letter_id),
            )
