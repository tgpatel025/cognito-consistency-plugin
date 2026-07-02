"""
Tests verifying:
  1. PostgresUserRepository (the shipped, runnable default) satisfies
     the UserRepository abstract interface -- i.e. every abstract
     method is implemented, so it can't be instantiated with a missing
     method that would only be discovered at runtime when some code
     path finally calls it.
  2. The partial worked example (example_custom_schema.py) correctly
     demonstrates its two mapping patterns (column renaming + reusing a
     generic table), even though it's intentionally incomplete and
     doesn't claim to satisfy the full interface -- see that module's
     docstring for why it stays partial.
  3. An incomplete UserRepository subclass fails to instantiate, which
     is what guarantees a custom implementation a developer writes
     can't silently omit a required method.
"""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.repositories.base import UserRepository
from common.repositories.postgres import PostgresUserRepository
from common.repositories.example_custom_schema import ExampleCustomSchemaRepositoryPartial


def _dummy_connect():
    raise AssertionError("connect_fn should not be called by instantiation alone")


def test_postgres_repository_implements_full_interface():
    repo = PostgresUserRepository(_dummy_connect)
    assert isinstance(repo, UserRepository)


def test_example_partial_repository_maps_column_names_correctly():
    """Exercises pattern 1 from example_custom_schema.py: get_all_users
    must translate this schema's own column names (cognito_id,
    email_address, display_name) into the interface's expected keys
    (cognito_sub, email, username), proving the interface doesn't
    assume any particular column naming."""
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


def test_example_partial_repository_is_intentionally_incomplete():
    """This class does NOT subclass UserRepository and is not meant to
    -- it's a partial teaching example, not a drop-in implementation.
    This test documents that choice so it doesn't look like an
    oversight if someone notices it's missing five interface methods."""
    assert not issubclass(ExampleCustomSchemaRepositoryPartial, UserRepository)


def test_cannot_instantiate_incomplete_userrepository_subclass():
    """A repository missing even one abstract method must fail to
    instantiate -- this is what guarantees a custom implementation a
    developer writes can't silently omit a required method and only
    find out when the reconciler or a Lambda handler crashes at
    runtime."""

    class IncompleteRepository(UserRepository):
        def upsert_user(self, cognito_sub, email, username, attributes):
            return {"id": 1, "inserted": True}

        # Deliberately missing every other abstract method.

    try:
        IncompleteRepository()
        assert False, "expected TypeError for incomplete UserRepository implementation"
    except TypeError as exc:
        assert "abstract" in str(exc).lower()
