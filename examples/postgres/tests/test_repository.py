"""
Tests verifying the Postgres example (repository.py) actually satisfies
the UserRepository interface. Lives alongside the example, not in the
core tests/ directory, since PostgresUserRepository is example code, not
part of the core library -- see docs/extending-the-repository.md.

Run with: pytest examples/postgres/tests/ (requires
examples/postgres/requirements.txt installed).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from common.repositories.base import UserRepository
from postgres.repository import PostgresUserRepository


def _dummy_connect():
    raise AssertionError("connect_fn should not be called by instantiation alone")


def test_postgres_repository_implements_full_interface():
    repo = PostgresUserRepository(_dummy_connect)
    assert isinstance(repo, UserRepository)


def test_postgres_repository_defaults_to_its_own_connection_module_if_no_connect_fn_given():
    """PostgresUserRepository() with no args should fall back to this
    example's own connection.get_connection -- this is what makes
    REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"
    work with the factory's zero-argument instantiation
    (common/service_factory.py no longer passes a connect_fn to any
    repository it loads)."""
    repo = PostgresUserRepository()
    # Comparing by name since the imported function objects may differ
    # by identity depending on how connection.py was imported (relative
    # vs. path-inserted) across test runs.
    assert repo._connect_fn.__name__ == "get_connection"
