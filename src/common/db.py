"""
Shared database access layer.

Design notes
------------
- Uses psycopg2 with a simple connection-per-invocation pattern, which is
  fine for Lambda (short-lived, low concurrency demo). For production,
  swap in RDS Proxy or a connection pool (e.g. pgbouncer) to avoid
  exhausting Postgres connections under concurrent Lambda invocations.
- All writes are idempotent (upsert on cognito_sub) so retries from
  Cognito's own Lambda retry behavior, or from the reconciler's replay
  path, never create duplicate or conflicting state.
"""

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_connection():
    """
    Create a new Postgres connection from environment variables.

    Expected env vars: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
    In production, DB_PASSWORD should come from Secrets Manager, not
    a plaintext env var. Kept simple here for the demo.
    """
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "identity_platform"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", "postgres"),
        connect_timeout=5,
    )


@contextmanager
def db_cursor(commit=True):
    conn = get_connection()
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


def upsert_user(cognito_sub, email, username, attributes, event_source):
    """
    Idempotent upsert keyed on cognito_sub (the immutable Cognito user id).

    Returns the row id and whether this was an insert or update, which the
    caller uses for audit logging.
    """
    now = datetime.now(timezone.utc)
    with db_cursor() as cur:
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

    log_sync_event(
        cognito_sub=cognito_sub,
        event_source=event_source,
        status="success",
        detail="insert" if row["inserted"] else "update",
    )
    return row


def log_sync_event(cognito_sub, event_source, status, detail=None):
    """Append-only audit trail. Never updated, only inserted -- this is
    what the audit/compliance visibility feature is built on."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_audit_log (cognito_sub, event_source, status, detail, occurred_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (cognito_sub, event_source, status, detail, datetime.now(timezone.utc)),
        )


def get_all_app_users():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT cognito_sub, email, username, attributes, last_synced_at FROM app_users")
        return cur.fetchall()


def enqueue_dead_letter(cognito_sub, payload, error):
    """Record a failed sync for later replay. Complements the SQS DLQ:
    this table is queryable/reportable, the SQS DLQ is what actually
    retries delivery."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_dead_letters (cognito_sub, payload, error, occurred_at, replayed)
            VALUES (%s, %s, %s, %s, false)
            """,
            (cognito_sub, json.dumps(payload), str(error), datetime.now(timezone.utc)),
        )
