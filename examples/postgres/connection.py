"""
Connection helper for the Postgres example repository. Example-owned,
not core: the core library ships zero database dependencies (see
docs/extending-the-repository.md). Adapting this example? Keep a helper
shaped like this (or use a pool / your ORM's sessions) and pass it into
your repository's constructor like PostgresUserRepository's connect_fn.

Design notes
------------
- Connection-per-invocation: fine for a Lambda demo. For production, use
  RDS Proxy or pgbouncer so concurrent Lambdas don't exhaust Postgres.
- Credentials: DB_SECRET_ARN set -> Secrets Manager. Not set -> plaintext
  DB_* env vars, but only with ALLOW_PLAINTEXT_DB_CREDS=1 (the local/
  LocalStack path, see docs/local-demo.md). Without that flag it raises:
  a missing DB_SECRET_ARN in a real deployment is almost always a
  misconfig, and failing loud beats silently connecting with defaults.
"""

import logging
import os
import json
from functools import lru_cache

import psycopg2

logger = logging.getLogger()


@lru_cache(maxsize=1)
def _fetch_secret(secret_arn: str) -> dict:
    """Fetch DB credentials from Secrets Manager, cached for the Lambda
    environment's lifetime (warm starts reuse it; re-fetching every
    invocation is latency+cost for nothing). Credential rotation is
    picked up when the environment recycles."""
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def get_connection():
    """New Postgres connection: Secrets Manager (DB_SECRET_ARN) or
    plaintext env vars (local fallback, see module docstring). Passed as
    connect_fn to PostgresUserRepository."""
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

    if os.environ.get("ALLOW_PLAINTEXT_DB_CREDS") != "1":
        raise RuntimeError(
            "DB_SECRET_ARN not set. Refusing to fall back to plaintext env-var "
            "credentials -- in production this is almost always a misconfig. "
            "For local/LocalStack runs, set ALLOW_PLAINTEXT_DB_CREDS=1 "
            "(see docs/local-demo.md)."
        )

    logger.warning(
        "DB_SECRET_ARN not set; using plaintext DB_* env vars "
        "(ALLOW_PLAINTEXT_DB_CREDS=1). Local/LocalStack use only."
    )
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "identity_platform"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", "postgres"),
        connect_timeout=5,
    )
