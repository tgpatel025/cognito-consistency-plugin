"""
Tests for the UserRepository interface contract itself
(src/common/repositories/base.py) -- independent of any example
implementation, since the core library ships no repository at all.

This is the test that would catch someone adding a new abstract method
to UserRepository and forgetting to implement it somewhere -- Python's
ABC machinery raises TypeError at instantiation time if any
@abstractmethod is unimplemented.

Example-specific tests (verifying examples/postgres/repository.py or
examples/custom_schema_partial/repository.py behave correctly) live
alongside those examples, not here -- see examples/postgres/tests/.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.repositories.base import UserRepository


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


def test_complete_userrepository_subclass_can_be_instantiated():
    """The positive case: implementing every abstract method allows
    instantiation, and the result is a real UserRepository instance."""

    class CompleteRepository(UserRepository):
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

    repo = CompleteRepository()
    assert isinstance(repo, UserRepository)
