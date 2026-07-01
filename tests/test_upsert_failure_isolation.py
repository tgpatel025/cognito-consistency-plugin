"""
Tests for the failure-isolation behavior in upsert_user: an audit-log
write failure must never be reported as a sync failure, since the
actual important write (app_users) already succeeded by the time the
audit log is written.
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.db import upsert_user


def make_fake_cursor(returned_row):
    """Mimics the db_cursor() context manager's cursor for the
    app_users INSERT ... RETURNING call."""
    cursor = MagicMock()
    cursor.fetchone.return_value = returned_row
    return cursor


def test_upsert_succeeds_normally_when_audit_log_also_succeeds():
    fake_row = {"id": 1, "inserted": True}

    with patch("common.db.get_connection") as mock_get_conn:
        mock_conn = MagicMock()
        mock_cursor = make_fake_cursor(fake_row)
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        result = upsert_user(
            cognito_sub="sub-1", email="a@example.com", username="alice",
            attributes={}, event_source="test",
        )

    assert result == fake_row


def test_upsert_still_returns_success_when_audit_log_write_fails():
    """The critical case: app_users write succeeds, but log_sync_event
    (a separate db_cursor()/connection) raises. upsert_user must still
    return the successful row, not raise, and not report failure."""
    fake_row = {"id": 1, "inserted": True}

    with patch("common.db.get_connection") as mock_get_conn:
        mock_conn = MagicMock()
        mock_cursor = make_fake_cursor(fake_row)
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        with patch("common.db.log_sync_event", side_effect=Exception("audit table full")):
            result = upsert_user(  # must not raise
                cognito_sub="sub-1", email="a@example.com", username="alice",
                attributes={}, event_source="test",
            )

    # The app_users write succeeded -- upsert_user must reflect that,
    # regardless of the audit log failure.
    assert result == fake_row


def test_upsert_does_not_call_log_sync_event_if_app_users_write_itself_fails():
    """If the primary write fails, we should never even attempt the
    audit log call for a row that doesn't exist -- upsert_user should
    raise from the app_users failure itself, not proceed to audit
    logging with a row that was never fetched."""
    with patch("common.db.get_connection") as mock_get_conn:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("db down")
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        with patch("common.db.log_sync_event") as mock_log:
            try:
                upsert_user(
                    cognito_sub="sub-1", email="a@example.com", username="alice",
                    attributes={}, event_source="test",
                )
                assert False, "expected upsert_user to raise when the app_users write fails"
            except Exception:
                pass

        mock_log.assert_not_called()
