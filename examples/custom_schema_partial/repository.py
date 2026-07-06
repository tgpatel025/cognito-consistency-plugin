"""
Worked example (partial) of a UserRepository against a pre-existing
schema that looks nothing like the Postgres example's -- proving the
interface is schema-agnostic, not a rename of the same three tables.

Imagined pre-existing schema (typical "we had a users table before we
ever heard of this project" shape):

    CREATE TABLE users (
        id            SERIAL PRIMARY KEY,      -- app's own integer PK, NOT cognito_sub
        cognito_id    TEXT UNIQUE,              -- cognito_sub lives here, as a nullable column
        email_address TEXT,                      -- different column name than 'email'
        display_name  TEXT,                      -- different column name than 'username'
        metadata      JSONB DEFAULT '{}',
        updated_at    TIMESTAMPTZ DEFAULT now()
    );

    -- a generic failed-jobs table the app already had, reused here for
    -- dead letters instead of a dedicated sync_dead_letters table
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

Implements only upsert_user, get_all_users, and enqueue_dead_letter --
the two mapping patterns that matter:

  1. Column renaming + different primary key: `cognito_id` as the unique
     key, `email_address`/`display_name` for `email`/`username`;
     get_all_users() translates back to the interface's dict keys.
  2. Reusing a generic existing table (`failed_jobs`, filtered by
     `job_type = 'cognito_sync'`) instead of a dedicated one.

The other five interface methods are the same two patterns applied to
`failed_jobs` -- see examples/postgres/repository.py for full logic;
only table/column names change. Kept partial on purpose: a complete
copy would just be that file with different names, drifting out of sync.

Not meant to run as-is -- your real schema will differ. Copy the
patterns. Connection setup is also entirely yours (see
docs/extending-the-repository.md, "Constructor signature").
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras


class ExampleCustomSchemaRepositoryPartial:
    """Partial implementation -- see module docstring. Does not
    subclass UserRepository directly, since it deliberately omits most
    of the interface; instantiating a real implementation with missing
    methods would raise (see tests/test_repository_interface.py for
    that behavior against complete implementations)."""

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

    # -- Pattern 1: column renaming + a different primary key ------------

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
                "SELECT cognito_id, email_address, display_name, metadata, updated_at "
                "FROM users WHERE cognito_id IS NOT NULL"
            )
            rows = cur.fetchall()

        # The interface requires cognito_sub/email/username keys regardless
        # of what this schema calls them -- translate here, once.
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

    # -- Pattern 2: reusing an existing generic table -----------------

    def enqueue_dead_letter(self, cognito_sub: str, payload: dict, error: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO failed_jobs (job_type, reference_id, job_data, error_message, attempts, resolved, created_at)
                VALUES ('cognito_sync', %s, %s, %s, 0, false, %s)
                """,
                (cognito_sub, json.dumps(payload), str(error), datetime.now(timezone.utc)),
            )

    # log_sync_event, fetch_unreplayed_dead_letters, fetch_stuck_dead_letters,
    # mark_dead_letter_replayed, and record_dead_letter_failure would follow
    # the same two patterns above, querying/updating `failed_jobs` filtered
    # by job_type = 'cognito_sync' -- omitted here to keep this example
    # focused on the patterns rather than duplicating the Postgres
    # example's full structure under different names. See
    # examples/postgres/repository.py for their complete logic against
    # that example's reference schema.
