"""
Tests for common/service_factory.py: the single place that decides
which UserRepository implementation backs SyncService.

Covers:
  - REPOSITORY_CLASS unset -> raises RuntimeError immediately, with a
    clear, actionable message (there is no default repository)
  - REPOSITORY_CLASS set -> loads the class by dotted "module:ClassName"
    string and constructs it with zero arguments
  - malformed REPOSITORY_CLASS values raise a clear ValueError
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import common.service_factory as service_factory
from common.repositories.base import UserRepository


class _FakeCompleteRepository(UserRepository):
    """A minimal, complete, zero-argument-constructible UserRepository
    used only to test that service_factory correctly loads and
    instantiates a custom class."""

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


# Registered into sys.modules so service_factory's importlib-based
# loader can find it via a dotted path, the same way it would find a
# real third-party module bundled into the Lambda deployment package.
_fake_module = type(sys)("test_fake_complete_repo_module")
_fake_module.FakeCompleteRepository = _FakeCompleteRepository
sys.modules["test_fake_complete_repo_module"] = _fake_module


def test_raises_clear_error_when_repository_class_not_set():
    """There is no default repository -- this must fail loudly and
    immediately, not silently fall back to any particular database."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REPOSITORY_CLASS", None)
        try:
            service_factory.build_sync_service()
            assert False, "expected RuntimeError when REPOSITORY_CLASS is unset"
        except RuntimeError as exc:
            assert "REPOSITORY_CLASS is not set" in str(exc)
            assert "UserRepository" in str(exc)


def test_custom_repository_class_is_loaded_and_constructed_with_no_arguments():
    with patch.dict(os.environ, {
        "REPOSITORY_CLASS": "test_fake_complete_repo_module:FakeCompleteRepository"
    }):
        service = service_factory.build_sync_service()

    assert isinstance(service.repository, _FakeCompleteRepository)
    assert service.repository.constructed is True


def test_malformed_repository_class_raises_clear_error():
    with patch.dict(os.environ, {"REPOSITORY_CLASS": "not-a-valid-dotted-path"}):
        try:
            service_factory.build_sync_service()
            assert False, "expected ValueError for malformed REPOSITORY_CLASS"
        except ValueError as exc:
            assert "module.path:ClassName" in str(exc)


def test_repository_requiring_constructor_arguments_raises_clear_typeerror():
    """The factory always constructs with zero arguments now -- a
    repository whose constructor requires arguments (e.g. the old
    connect_fn pattern) must fail with a clear TypeError, not
    something the factory silently papers over. Repositories needing
    setup should default their own arguments internally (see
    examples/postgres/repository.py's connect_fn=None pattern) or read
    from env vars/module-level config themselves."""

    class RequiresArgRepository(UserRepository):
        def __init__(self, something_required):
            pass

        def upsert_user(self, cognito_sub, email, username, attributes):
            return {}

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

    module = type(sys)("test_requires_arg_repo_module")
    module.RequiresArgRepository = RequiresArgRepository
    sys.modules["test_requires_arg_repo_module"] = module

    with patch.dict(os.environ, {"REPOSITORY_CLASS": "test_requires_arg_repo_module:RequiresArgRepository"}):
        try:
            service_factory.build_sync_service()
            assert False, "expected TypeError for a repository requiring constructor arguments"
        except TypeError:
            pass

    del sys.modules["test_requires_arg_repo_module"]
