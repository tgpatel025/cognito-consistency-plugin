"""
Tests for the partial custom-schema example (repository.py in the
parent directory) -- verifies the two mapping patterns it demonstrates
(column renaming, reusing a generic table) actually work.

Lives alongside the example, not in the core tests/ directory, since
this file is intentionally partial example code, not part of the core
library -- see docs/extending-the-repository.md and this example's own
module docstring for why it stays partial.
"""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_schema_partial.repository import ExampleCustomSchemaRepositoryPartial


def test_maps_column_names_correctly():
    """Pattern 1: get_all_users must translate this schema's own column
    names (cognito_id, email_address, display_name) into the
    interface's expected keys (cognito_sub, email, username)."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        {
            "cognito_id": "sub-123",
            "email_address": "a@example.com",
            "display_name": "alice",
            "metadata": {"custom": "value"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    repo = ExampleCustomSchemaRepositoryPartial(lambda: mock_conn)
    users = repo.get_all_users()

    assert users == [
        {
            "cognito_sub": "sub-123",
            "email": "a@example.com",
            "username": "alice",
            "attributes": {"custom": "value"},
            "last_synced_at": "2026-01-01T00:00:00Z",
        }
    ]


def test_upsert_user_writes_to_the_renamed_columns():
    """Pattern 1, write side: upsert_user must target this schema's
    actual column names (cognito_id, not cognito_sub)."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"id": 1, "inserted": True}
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    repo = ExampleCustomSchemaRepositoryPartial(lambda: mock_conn)
    result = repo.upsert_user(
        cognito_sub="sub-123", email="a@example.com", username="alice", attributes={},
    )

    assert result == {"id": 1, "inserted": True}
    executed_sql = mock_cursor.execute.call_args[0][0]
    assert "INTO users" in executed_sql
    assert "cognito_id" in executed_sql


def test_enqueue_dead_letter_reuses_the_generic_failed_jobs_table():
    """Pattern 2: dead letters go into the generic, pre-existing
    failed_jobs table (filtered by job_type), not a dedicated table."""
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    repo = ExampleCustomSchemaRepositoryPartial(lambda: mock_conn)
    repo.enqueue_dead_letter(cognito_sub="sub-123", payload={"email": "a@example.com"}, error="boom")

    executed_sql = mock_cursor.execute.call_args[0][0]
    assert "failed_jobs" in executed_sql
    assert "cognito_sync" in executed_sql


def test_is_intentionally_not_a_full_userrepository():
    """This class does NOT subclass UserRepository and is not meant to
    -- it's a partial teaching example. This test documents that choice
    so it doesn't look like an oversight if someone notices it's
    missing five interface methods."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
    from common.repositories.base import UserRepository

    assert not issubclass(ExampleCustomSchemaRepositoryPartial, UserRepository)
