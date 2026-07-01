"""
Tests for SyncService.sync_user's failure-isolation behavior: an
audit-log write failure must never be reported as a sync failure, since
the actual important write (the user upsert) already succeeded by the
time the audit log is written.

This logic moved from common/db.py::upsert_user directly into
SyncService when the repository interface was introduced (see
common/repositories/base.py) -- it's tested here against a fake
UserRepository rather than a real Postgres connection, which is exactly
the point of the interface: this cross-cutting behavior should hold
regardless of which repository implementation is plugged in.
"""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.sync_service import SyncService


def test_sync_user_succeeds_normally_when_audit_log_also_succeeds():
    fake_repo = MagicMock()
    fake_repo.upsert_user.return_value = {"id": 1, "inserted": True}

    service = SyncService(fake_repo)
    result = service.sync_user(
        cognito_sub="sub-1", email="a@example.com", username="alice",
        attributes={}, event_source="test",
    )

    assert result == {"id": 1, "inserted": True}
    fake_repo.log_sync_event.assert_called_once()


def test_sync_user_still_returns_success_when_audit_log_write_fails():
    """The critical case: the repository's upsert_user succeeds, but
    log_sync_event raises. sync_user must still return the successful
    result, not raise, and not report failure -- regardless of which
    UserRepository implementation is behind it."""
    fake_repo = MagicMock()
    fake_repo.upsert_user.return_value = {"id": 1, "inserted": True}
    fake_repo.log_sync_event.side_effect = Exception("audit table full")

    service = SyncService(fake_repo)
    result = service.sync_user(  # must not raise
        cognito_sub="sub-1", email="a@example.com", username="alice",
        attributes={}, event_source="test",
    )

    assert result == {"id": 1, "inserted": True}


def test_sync_user_does_not_call_log_sync_event_if_upsert_itself_fails():
    """If the primary write fails, sync_user should raise from that
    failure directly and never attempt to log an audit event for a
    write that never happened."""
    fake_repo = MagicMock()
    fake_repo.upsert_user.side_effect = Exception("db down")

    service = SyncService(fake_repo)

    try:
        service.sync_user(
            cognito_sub="sub-1", email="a@example.com", username="alice",
            attributes={}, event_source="test",
        )
        assert False, "expected sync_user to raise when upsert_user fails"
    except Exception:
        pass

    fake_repo.log_sync_event.assert_not_called()


def test_sync_user_passes_insert_vs_update_detail_correctly():
    fake_repo = MagicMock()
    fake_repo.upsert_user.return_value = {"id": 1, "inserted": False}

    service = SyncService(fake_repo)
    service.sync_user(
        cognito_sub="sub-1", email="a@example.com", username="alice",
        attributes={}, event_source="test",
    )

    _, kwargs = fake_repo.log_sync_event.call_args
    assert kwargs["detail"] == "update"
    assert kwargs["status"] == "success"
