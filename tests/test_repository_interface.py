"""
Tests verifying that both shipped repository implementations
(PostgresUserRepository and the worked example,
ExampleCustomSchemaRepository) actually satisfy the UserRepository
abstract interface -- i.e. every abstract method is implemented, so
neither can be instantiated with a missing method that would only be
discovered at runtime when some code path finally calls it.

This is the test that would catch someone adding a new abstract method
to UserRepository and forgetting to implement it somewhere -- Python's
ABC machinery raises TypeError at instantiation time if any
@abstractmethod is unimplemented, which these tests exercise directly.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.repositories.base import UserRepository
from common.repositories.postgres import PostgresUserRepository
from common.repositories.example_custom_schema import ExampleCustomSchemaRepository


def _dummy_connect():
    raise AssertionError("connect_fn should not be called by instantiation alone")


def test_postgres_repository_implements_full_interface():
    repo = PostgresUserRepository(_dummy_connect)
    assert isinstance(repo, UserRepository)


def test_example_custom_schema_repository_implements_full_interface():
    repo = ExampleCustomSchemaRepository(_dummy_connect)
    assert isinstance(repo, UserRepository)


def test_cannot_instantiate_incomplete_repository():
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
