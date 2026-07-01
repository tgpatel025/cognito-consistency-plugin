"""
Tests the invariant that matters most for these handlers: they must
NEVER raise, no matter what fails internally -- because an uncaught
exception here blocks the user's sign-up/sign-in in Cognito.

Uses unittest.mock to simulate total DB unavailability (both the primary
sync AND the dead-letter/audit fallback failing), which is the realistic
worst case: if the database is down, every call to it fails the same
way.

Since the repository refactor, handlers depend on a module-level
_sync_service instance (built via common.service_factory.build_sync_service)
rather than importing individual db functions directly -- so these tests
patch methods on that instance instead of patching free functions. This
also means these tests exercise the SAME code path regardless of which
UserRepository is actually configured, which is the point of the
interface.
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
        with patch.object(post_confirmation._sync_service, "sync_user") as mock_sync:
            result = post_confirmation.handler(event, context=None)
        mock_sync.assert_called_once()
        assert result == event

    def test_sync_fails_dead_letter_succeeds_still_returns_event(self):
        event = make_event("PostConfirmation_ConfirmSignUp")
        with patch.object(post_confirmation._sync_service, "sync_user", side_effect=Exception("db down")), \
             patch.object(post_confirmation._sync_service, "enqueue_dead_letter") as mock_dl, \
             patch.object(post_confirmation._sync_service, "log_failure") as mock_log:
            result = post_confirmation.handler(event, context=None)

        mock_dl.assert_called_once()
        mock_log.assert_called_once()
        assert result == event

    def test_everything_fails_handler_still_does_not_raise(self):
        """The critical case: sync_user fails AND the fallback dead-letter/
        audit writes also fail (e.g. total database outage). The handler
        must still return normally, not propagate any exception."""
        event = make_event("PostConfirmation_ConfirmSignUp")
        with patch.object(post_confirmation._sync_service, "sync_user", side_effect=Exception("db down")), \
             patch.object(post_confirmation._sync_service, "enqueue_dead_letter", side_effect=Exception("db still down")), \
             patch.object(post_confirmation._sync_service, "log_failure", side_effect=Exception("db still down")):
            result = post_confirmation.handler(event, context=None)  # must not raise

        assert result == event

    def test_ignores_unrelated_trigger_sources(self):
        event = make_event("PreSignUp_SignUp")
        with patch.object(post_confirmation._sync_service, "sync_user") as mock_sync:
            result = post_confirmation.handler(event, context=None)
        mock_sync.assert_not_called()
        assert result == event


class TestPostAuthenticationNeverRaises:
    def test_normal_success_returns_event_unmodified(self):
        event = make_event("PostAuthentication_Authentication")
        with patch.object(post_authentication._sync_service, "sync_user") as mock_sync:
            result = post_authentication.handler(event, context=None)
        mock_sync.assert_called_once()
        assert result == event

    def test_everything_fails_handler_still_does_not_raise(self):
        event = make_event("PostAuthentication_Authentication")
        with patch.object(post_authentication._sync_service, "sync_user", side_effect=Exception("db down")), \
             patch.object(post_authentication._sync_service, "enqueue_dead_letter", side_effect=Exception("db still down")), \
             patch.object(post_authentication._sync_service, "log_failure", side_effect=Exception("db still down")):
            result = post_authentication.handler(event, context=None)  # must not raise

        assert result == event
