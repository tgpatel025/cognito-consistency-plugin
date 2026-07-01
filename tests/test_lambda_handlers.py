"""
Tests the invariant that matters most for these handlers: they must
NEVER raise, no matter what fails internally -- because an uncaught
exception here blocks the user's sign-up/sign-in in Cognito.

Uses unittest.mock to simulate total DB unavailability (both the primary
sync AND the dead-letter/audit fallback failing), which is the realistic
worst case: if Postgres is down, every DB call fails the same way.
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lambdas.post_confirmation import handler as post_confirmation
from lambdas.post_authentication import handler as post_authentication


def make_event(trigger_source, sub="sub-123", email="a@example.com", username="alice"):
    return {
        "triggerSource": trigger_source,
        "userName": username,
        "request": {
            "userAttributes": {"sub": sub, "email": email},
        },
    }


class TestPostConfirmationNeverRaises:
    def test_normal_success_returns_event_unmodified(self):
        event = make_event("PostConfirmation_ConfirmSignUp")
        with patch("lambdas.post_confirmation.handler.upsert_user") as mock_upsert:
            result = post_confirmation.handler(event, context=None)
        mock_upsert.assert_called_once()
        assert result == event

    def test_upsert_fails_dead_letter_succeeds_still_returns_event(self):
        event = make_event("PostConfirmation_ConfirmSignUp")
        with patch("lambdas.post_confirmation.handler.upsert_user", side_effect=Exception("db down")), \
             patch("lambdas.post_confirmation.handler.enqueue_dead_letter") as mock_dl, \
             patch("lambdas.post_confirmation.handler.log_sync_event") as mock_log:
            result = post_confirmation.handler(event, context=None)

        mock_dl.assert_called_once()
        mock_log.assert_called_once()
        assert result == event

    def test_everything_fails_handler_still_does_not_raise(self):
        """The critical case: upsert_user fails AND the fallback dead-letter/
        audit writes also fail (e.g. total Postgres outage). The handler
        must still return normally, not propagate any exception."""
        event = make_event("PostConfirmation_ConfirmSignUp")
        with patch("lambdas.post_confirmation.handler.upsert_user", side_effect=Exception("db down")), \
             patch("lambdas.post_confirmation.handler.enqueue_dead_letter", side_effect=Exception("db still down")), \
             patch("lambdas.post_confirmation.handler.log_sync_event", side_effect=Exception("db still down")):
            result = post_confirmation.handler(event, context=None)  # must not raise

        assert result == event

    def test_ignores_unrelated_trigger_sources(self):
        event = make_event("PreSignUp_SignUp")
        with patch("lambdas.post_confirmation.handler.upsert_user") as mock_upsert:
            result = post_confirmation.handler(event, context=None)
        mock_upsert.assert_not_called()
        assert result == event


class TestPostAuthenticationNeverRaises:
    def test_normal_success_returns_event_unmodified(self):
        event = make_event("PostAuthentication_Authentication")
        with patch("lambdas.post_authentication.handler.upsert_user") as mock_upsert:
            result = post_authentication.handler(event, context=None)
        mock_upsert.assert_called_once()
        assert result == event

    def test_everything_fails_handler_still_does_not_raise(self):
        event = make_event("PostAuthentication_Authentication")
        with patch("lambdas.post_authentication.handler.upsert_user", side_effect=Exception("db down")), \
             patch("lambdas.post_authentication.handler.enqueue_dead_letter", side_effect=Exception("db still down")), \
             patch("lambdas.post_authentication.handler.log_sync_event", side_effect=Exception("db still down")):
            result = post_authentication.handler(event, context=None)  # must not raise

        assert result == event
