"""
PostgresUserRepository: an EXAMPLE implementation of UserRepository,
matching the schema in schema.sql in this same directory
(app_users / sync_audit_log / sync_dead_letters).

This is not part of the core library. The core library
(src/common/repositories/base.py) ships zero database drivers and zero
default repository -- see docs/extending-the-repository.md for why.
This example exists so:
  - the LocalStack demo (infra/localstack) has something concrete to run
  - anyone who wants a working starting point can copy this directory
    and adapt it, rather than writing a UserRepository from a blank page

To use this example, install its own requirements.txt (psycopg2-binary
is NOT part of the core project's dependencies) and either import it
directly or set REPOSITORY_CLASS to
"examples.postgres.repository:PostgresUserRepository" with this
directory on your Python path.

See examples/custom_schema_partial/repository.py for a second example
against a deliberately different, non-Postgres-shaped schema, proving
the interface doesn't assume this one's table/column names.
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

# This example lives outside src/ (see docs/extending-the-repository.md
# for why the core library and examples are kept in separate top-level
# directories). Add src/ to the path so UserRepository can be imported
# without requiring this example to be installed as part of the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from common.repositories.base import UserRepository  # noqa: E402

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PostgresUserRepository(UserRepository):
    def __init__(self, connect_fn=None):
        """connect_fn: a zero-argument callable returning a new
        psycopg2 connection. Defaults to this example's own
        connection.get_connection (Secrets Manager or plaintext env
        vars) if not provided -- this default exists so
        REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"
        works with the factory's zero-argument instantiation
        (see common/service_factory.py). Pass an explicit connect_fn to
        override this, e.g. in tests."""
        if connect_fn is None:
            try:
                from .connection import get_connection
            except ImportError:
                # Relative import fails if this module was imported
                # directly (e.g. `import repository` after adding this
                # directory to sys.path) rather than as part of a
                # package (e.g. via importlib.import_module on
                # "examples.postgres.repository", which is how
                # service_factory.py loads REPOSITORY_CLASS). Fall back
                # to a path-based import so this constructor works
                # either way.
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
