"""
Tests for common/service_factory.py: the single place that decides
which UserRepository implementation backs SyncService.

Covers:
  - default path (no REPOSITORY_CLASS set) uses PostgresUserRepository
  - custom path loads a class by dotted "module:ClassName" string
  - custom classes with either constructor signature (connect_fn arg,
    or no-arg) both work
  - malformed REPOSITORY_CLASS values raise a clear error rather than
    an opaque one
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import common.service_factory as service_factory
from common.repositories.postgres import PostgresUserRepository
from common.repositories.base import UserRepository


def test_default_repository_is_postgres_when_no_repository_class_set():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REPOSITORY_CLASS", None)
        service = service_factory.build_sync_service()

    assert isinstance(service.repository, PostgresUserRepository)


def test_custom_repository_class_is_loaded_and_used():
    # Reuse the shipped example as a stand-in "custom" repository for
    # this test, since it's a real, importable UserRepository
    # implementation distinct from PostgresUserRepository.
    with patch.dict(os.environ, {
        "REPOSITORY_CLASS": "common.repositories.example_custom_schema:ExampleCustomSchemaRepository"
    }):
        service = service_factory.build_sync_service()

    from common.repositories.example_custom_schema import ExampleCustomSchemaRepository
    assert isinstance(service.repository, ExampleCustomSchemaRepository)


def test_custom_repository_with_no_arg_constructor_is_supported():
    """Some implementations manage their own connection setup and take
    no constructor arguments (e.g. a DynamoDB repository using boto3's
    default credential chain rather than a psycopg2 connect_fn)."""

    class NoArgRepository(UserRepository):
        def __init__(self):
            self.constructed = True

        def upsert_user(self, cognito_sub, email, username, attributes):
            return {"id": 1, "inserted": True}

        def get_all_users(self):
            return []

        def log_sync_event(self, cognito_sub, event_source, status, detail=None):
            pass

        def enqueue_dead_letter(self, cognito_sub, payload, error):
            pass

        def fetch_unreplayed_dead_letters(self, max_retry):
            return []

        def fetch_stuck_dead_letters(self, max_retry):
            return []

        def mark_dead_letter_replayed(self, dead_letter_id):
            pass

        def record_dead_letter_failure(self, dead_letter_id, error):
            pass

    # Inject the class into a real module namespace so the dotted-path
    # loader can import it.
    sys.modules["test_no_arg_repo_module"] = type(sys)("test_no_arg_repo_module")
    sys.modules["test_no_arg_repo_module"].NoArgRepository = NoArgRepository

    with patch.dict(os.environ, {"REPOSITORY_CLASS": "test_no_arg_repo_module:NoArgRepository"}):
        service = service_factory.build_sync_service()

    assert isinstance(service.repository, NoArgRepository)
    assert service.repository.constructed is True

    del sys.modules["test_no_arg_repo_module"]


def test_malformed_repository_class_raises_clear_error():
    with patch.dict(os.environ, {"REPOSITORY_CLASS": "not-a-valid-dotted-path"}):
        try:
            service_factory.build_sync_service()
            assert False, "expected ValueError for malformed REPOSITORY_CLASS"
        except ValueError as exc:
            assert "module.path:ClassName" in str(exc)
