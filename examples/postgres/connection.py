"""
Connection helper for the Postgres example repository.

This used to live in the core library (common/db.py) as a supposedly
"schema-independent" connection layer -- but it was never actually
engine-independent, since it imports psycopg2 directly. Anything that
picks a database driver is an opinion the core library shouldn't ship,
even if the schema on top of it is pluggable. It now lives here, fully
owned by this example, alongside its own requirements.txt
(psycopg2-binary) so the core library has zero database dependencies at
all -- see docs/extending-the-repository.md.

If you're adapting this example for your own schema, you'll typically
keep a connection helper shaped like this (or write your own, or use a
connection pool / your ORM's session management / whatever fits your
stack) and pass it into your repository's constructor the same way
PostgresUserRepository accepts connect_fn here.

Design notes
------------
- Uses psycopg2 with a simple connection-per-invocation pattern, which is
  fine for Lambda (short-lived, low concurrency demo). For production,
  swap in RDS Proxy or a connection pool (e.g. pgbouncer) to avoid
  exhausting Postgres connections under concurrent Lambda invocations.
- Credentials: if DB_SECRET_ARN is set, connection details are fetched
  from Secrets Manager. If DB_SECRET_ARN is not set, falls back to
  plaintext DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env vars, which
  is what the LocalStack/local-demo path uses (see docs/local-demo.md)
  since it avoids needing a real Secrets Manager round-trip for a quick
  local run.
"""

import os
import json
from functools import lru_cache

import psycopg2


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

    Passed as the connect_fn to PostgresUserRepository's constructor.
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
