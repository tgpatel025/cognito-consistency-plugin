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
- Credentials: if DB_SECRET_ARN is set, connection details are fetched
  from Secrets Manager (the path used by infra/terraform/module -- see
  its iam.tf for the exact, minimally-scoped secretsmanager:GetSecretValue
  permission each function is granted). If DB_SECRET_ARN is not set,
  falls back to plaintext DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env
  vars, which is what the LocalStack/local-demo path uses (see
  docs/local-demo.md) since it avoids needing a real Secrets Manager
  round-trip for a quick local run.
"""

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache

import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def _fetch_secret(secret_arn: str) -> dict:
    """Fetch and cache DB credentials from Secrets Manager for the
    lifetime of this Lambda execution environment. Cached because Lambda
    execution environments are reused across invocations (warm starts),
    and re-fetching the same secret on every invocation would add
    latency and cost for no benefit -- the secret is expected to be
    stable for the environment's lifetime; a credential rotation is
    picked up the next time the environment is recycled."""
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def get_connection():
    """
    Create a new Postgres connection, either from a Secrets Manager
    secret (DB_SECRET_ARN set) or from plaintext env vars (local/
    LocalStack fallback -- see module docstring above).
    """
    secret_arn = os.environ.get("DB_SECRET_ARN")

    if secret_arn:
        secret = _fetch_secret(secret_arn)
        return psycopg2.connect(
            host=secret["host"],
            port=secret.get("port", 5432),
            dbname=secret["dbname"],
            user=secret["username"],
            password=secret["password"],
            connect_timeout=5,
        )

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

    Design note: the app_users write and the audit-log write are
    deliberately two separate transactions (see log_sync_event), and the
    audit write is deliberately allowed to fail without affecting this
    function's return value or raising to the caller. The audit log
    exists to record what happened to app_users -- it should never be
    able to veto or mask the outcome of the write it's describing. If the
    audit write fails, we log loudly (it means the compliance trail has
    a gap) but the caller still sees upsert_user as successful, because
    it was: the user's data landed correctly.
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

    try:
        log_sync_event(
            cognito_sub=cognito_sub,
            event_source=event_source,
            status="success",
            detail="insert" if row["inserted"] else "update",
        )
    except Exception as exc:
        # The app_users write above already committed successfully. This
        # failure means only the audit trail has a gap for this event --
        # it must not be reported back as a sync failure, and must not
        # raise, or a caller might mistakenly enqueue a dead letter /
        # retry for a write that already succeeded.
        logger.error(
            "app_users upsert for %s succeeded, but audit log write failed: %s. "
            "This event will not appear in sync_audit_log.",
            cognito_sub,
            exc,
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
