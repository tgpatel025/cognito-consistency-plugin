"""
PostgresUserRepository: EXAMPLE implementation of UserRepository for the
schema in ./schema.sql (app_users / sync_audit_log / sync_dead_letters).

Not part of the core library (which ships zero DB drivers and no default
repository -- docs/extending-the-repository.md). Exists so the LocalStack
demo has something to run and so you can copy this directory instead of
starting from a blank page.

Use: install this directory's requirements.txt, then set
REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository".
See examples/custom_schema_partial/ for a second example against a
deliberately different schema.
"""

import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

# Example lives outside src/; add src/ to the path so UserRepository
# imports without installing this example as part of the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from common.repositories.base import UserRepository  # noqa: E402

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PostgresUserRepository(UserRepository):
    def __init__(self, connect_fn=None):
        """connect_fn: zero-arg callable returning a new psycopg2
        connection. Defaults to this example's connection.get_connection
        so the factory's zero-argument instantiation works; pass your
        own to override (e.g. in tests)."""
        if connect_fn is None:
            try:
                from .connection import get_connection
            except ImportError:
                # Relative import fails when this module is imported
                # directly (`import repository`) rather than as a package
                # (how service_factory loads REPOSITORY_CLASS). Path-based
                # fallback covers both.
                import sys as _sys
                import os as _os
                _sys.path.insert(0, _os.path.dirname(__file__))
                from connection import get_connection
            connect_fn = get_connection
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
        if not cognito_sub:
            raise ValueError("upsert_user requires a non-empty cognito_sub")

        now = datetime.now(timezone.utc)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_users (cognito_sub, email, username, attributes, last_synced_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cognito_sub)
                DO UPDATE SET
                    email = EXCLUDED.email,
                    username = EXCLUDED.username,
                    attributes = EXCLUDED.attributes,
                    last_synced_at = EXCLUDED.last_synced_at
                RETURNING id, (xmax = 0) AS inserted
                """,
                (cognito_sub, email, username, json.dumps(attributes), now, now),
            )
            row = cur.fetchone()
        return {"id": row["id"], "inserted": row["inserted"]}

    def get_all_users(self) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                "SELECT cognito_sub, email, username, attributes, last_synced_at FROM app_users"
            )
            return [dict(row) for row in cur.fetchall()]

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
                INSERT INTO sync_audit_log (cognito_sub, event_source, status, detail, occurred_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (cognito_sub, event_source, status, detail, datetime.now(timezone.utc)),
            )

    def enqueue_dead_letter(self, cognito_sub: str, payload: dict, error: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_dead_letters (cognito_sub, payload, error, occurred_at, replayed)
                VALUES (%s, %s, %s, %s, false)
                """,
                (cognito_sub, json.dumps(payload), str(error), datetime.now(timezone.utc)),
            )

    def fetch_unreplayed_dead_letters(self, max_retry: int) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT id, cognito_sub, payload, retry_count
                FROM sync_dead_letters
                WHERE replayed = false AND retry_count < %s
                ORDER BY occurred_at
                """,
                (max_retry,),
            )
            return [dict(row) for row in cur.fetchall()]

    def fetch_stuck_dead_letters(self, max_retry: int) -> list[dict]:
        with self._cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT id, cognito_sub, retry_count, last_error, occurred_at, last_attempted_at
                FROM sync_dead_letters
                WHERE replayed = false AND retry_count >= %s
                ORDER BY occurred_at
                """,
                (max_retry,),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_dead_letter_replayed(self, dead_letter_id: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sync_dead_letters SET replayed = true, replayed_at = %s WHERE id = %s",
                (datetime.now(timezone.utc), dead_letter_id),
            )

    def record_dead_letter_failure(self, dead_letter_id: Any, error: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE sync_dead_letters
                SET retry_count = retry_count + 1,
                    last_error = %s,
                    last_attempted_at = %s
                WHERE id = %s
                """,
                (str(error), datetime.now(timezone.utc), dead_letter_id),
            )
