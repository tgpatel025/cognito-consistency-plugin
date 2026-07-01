"""
PostgresUserRepository: the reference implementation of UserRepository,
matching the exact schema in infra/localstack/schema.sql
(app_users / sync_audit_log / sync_dead_letters).

This is meant to be runnable as-is (it's what the LocalStack demo and
the module's Lambdas use by default) AND to serve as a template for
writing your own repository against a different schema or engine -- see
docs/extending-the-repository.md for a guide, and
repositories/example_custom_schema.py for a worked example against a
differently-shaped, pre-existing `users` table.
"""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from common.repositories.base import UserRepository

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PostgresUserRepository(UserRepository):
    def __init__(self, connect_fn):
        """connect_fn: a zero-argument callable returning a new
        psycopg2 connection. Passed in rather than constructed here so
        credential-sourcing (Secrets Manager vs. plaintext env vars,
        see common/db.py) stays decoupled from the repository's SQL --
        the repository doesn't need to know or care where the
        connection came from."""
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
