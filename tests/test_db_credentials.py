"""
Tests for get_connection()'s two credential paths:
  1. DB_SECRET_ARN set -> fetch from Secrets Manager
  2. DB_SECRET_ARN unset -> plaintext env vars (local/LocalStack path)
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import common.db as db


def setup_function():
    # _fetch_secret is lru_cache'd -- clear between tests so one test's
    # mocked secret doesn't leak into another's assertions.
    db._fetch_secret.cache_clear()


def test_uses_plaintext_env_vars_when_no_secret_arn_set():
    env = {
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "identity_platform",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
    }
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("DB_SECRET_ARN", None)
        with patch("psycopg2.connect") as mock_connect:
            db.get_connection()

    mock_connect.assert_called_once_with(
        host="localhost", port="5432", dbname="identity_platform",
        user="postgres", password="postgres", connect_timeout=5,
    )


def test_uses_secrets_manager_when_secret_arn_set():
    secret_payload = {
        "host": "prod-db.example.internal",
        "port": 5432,
        "dbname": "app_production",
        "username": "svc_ccp",
        "password": "s3cr3t",
    }

    mock_sm_client = MagicMock()
    mock_sm_client.get_secret_value.return_value = {"SecretString": json.dumps(secret_payload)}

    with patch.dict(os.environ, {"DB_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds"}):
        with patch("boto3.client", return_value=mock_sm_client) as mock_boto_client, \
             patch("psycopg2.connect") as mock_connect:
            db.get_connection()

    mock_boto_client.assert_called_once_with("secretsmanager")
    mock_sm_client.get_secret_value.assert_called_once_with(
        SecretId="arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds"
    )
    mock_connect.assert_called_once_with(
        host="prod-db.example.internal", port=5432, dbname="app_production",
        user="svc_ccp", password="s3cr3t", connect_timeout=5,
    )


def test_secret_is_only_fetched_once_across_multiple_connections():
    """Regression guard for the lru_cache: repeated get_connection()
    calls within the same warm Lambda environment should not re-fetch
    the secret from Secrets Manager every time."""
    secret_payload = {
        "host": "prod-db.example.internal", "port": 5432,
        "dbname": "app_production", "username": "svc_ccp", "password": "s3cr3t",
    }
    mock_sm_client = MagicMock()
    mock_sm_client.get_secret_value.return_value = {"SecretString": json.dumps(secret_payload)}

    with patch.dict(os.environ, {"DB_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds"}):
        with patch("boto3.client", return_value=mock_sm_client), \
             patch("psycopg2.connect"):
            db.get_connection()
            db.get_connection()
            db.get_connection()

    assert mock_sm_client.get_secret_value.call_count == 1
