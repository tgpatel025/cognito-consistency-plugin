"""
Shared pytest configuration for the core test suite.

The Lambda handlers (src/lambdas/*/handler.py) build their SyncService
at import time (_sync_service = build_sync_service()), and
build_sync_service() now raises immediately if REPOSITORY_CLASS isn't
set (see src/common/service_factory.py) -- this is correct production
behavior (a misconfigured Lambda should fail loudly at cold-start, not
silently accept traffic), but it means test modules that import the
handlers need REPOSITORY_CLASS set before that import happens.

This conftest sets REPOSITORY_CLASS to a minimal, in-memory fake
repository (defined here, not tied to any real database) before test
collection begins, so importing the handler modules succeeds. Individual
tests then override the handler's _sync_service methods with mocks as
needed (see test_lambda_handlers.py) -- the fake repository here only
needs to exist and be importable, not do anything meaningful, since no
test actually exercises it directly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Must be set before any test module imports a Lambda handler.
os.environ.setdefault("REPOSITORY_CLASS", "tests.conftest:_NullRepositoryForImportOnly")


from common.repositories.base import UserRepository  # noqa: E402


class _NullRepositoryForImportOnly(UserRepository):
    """Exists only so REPOSITORY_CLASS resolves to something importable
    and complete during test collection. Every method raises if actually
    called -- no test should rely on this repository doing real work;
    tests that need specific behavior mock the relevant SyncService
    methods directly (see test_lambda_handlers.py, test_sync_service.py)."""

    def _unused(self, *args, **kwargs):
        raise AssertionError(
            "_NullRepositoryForImportOnly was actually invoked -- tests should "
            "mock SyncService/repository methods explicitly rather than relying "
            "on this fallback doing real work."
        )

    upsert_user = _unused
    get_all_users = _unused
    log_sync_event = _unused
    enqueue_dead_letter = _unused
    fetch_unreplayed_dead_letters = _unused
    fetch_stuck_dead_letters = _unused
    mark_dead_letter_replayed = _unused
    record_dead_letter_failure = _unused
