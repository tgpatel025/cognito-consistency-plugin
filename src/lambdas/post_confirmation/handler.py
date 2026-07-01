"""
Cognito Post Confirmation trigger.

Fires once, after a user confirms sign-up (or a forgot-password confirmation).
This is where the application-side user record is first created.

Failure handling
-----------------
Cognito invokes this trigger synchronously and expects a response within
5 seconds. If we raise an exception here, Cognito will NOT complete the
user's sign-up -- so we deliberately never raise. Instead, on failure we:
  1. write a dead-letter record for the reconciler to pick up and replay
  2. log a 'failure' audit event
  3. return the event unmodified so Cognito's confirmation flow proceeds

This means a DB outage never blocks user sign-up, but it does create
drift -- which is exactly what the reconciliation job is for.

Storage: this handler depends on SyncService (common/sync_service.py),
never on any specific database or schema directly. Which storage
backend SyncService uses is decided by common/service_factory.py --
defaults to the Postgres reference schema, or your own implementation
of UserRepository via the REPOSITORY_CLASS env var. See
docs/extending-the-repository.md.
"""

import logging

from common.service_factory import build_sync_service

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sync_service = build_sync_service()


def handler(event, context):
    if event.get("triggerSource") not in (
        "PostConfirmation_ConfirmSignUp",
        "PostConfirmation_ConfirmForgotPassword",
    ):
        return event

    attributes = event["request"]["userAttributes"]
    cognito_sub = attributes.get("sub")
    email = attributes.get("email")
    username = event.get("userName")

    try:
        _sync_service.sync_user(
            cognito_sub=cognito_sub,
            email=email,
            username=username,
            attributes=attributes,
            event_source="post_confirmation",
        )
        logger.info("Synced user %s to app database", cognito_sub)
    except Exception as exc:
        logger.error("Failed to sync user %s: %s", cognito_sub, exc)
        # The dead-letter/audit writes below go through the same
        # repository, so if sync_user failed because the database is
        # unreachable, these will likely fail too. They're wrapped
        # separately so that failure can never propagate out of the
        # handler -- the one invariant that must hold no matter what is
        # "this function never raises." Worst case here is a sync
        # failure we can't even record; that's an acceptable degradation
        # compared to blocking the user's sign-up.
        try:
            _sync_service.enqueue_dead_letter(cognito_sub=cognito_sub, payload=attributes, error=exc)
            _sync_service.log_failure(
                cognito_sub=cognito_sub, event_source="post_confirmation", detail=str(exc),
            )
        except Exception as inner_exc:
            logger.critical(
                "Failed to record dead-letter/audit for user %s after sync failure: %s. "
                "This event is now unrecoverable except via Cognito's own user record.",
                cognito_sub,
                inner_exc,
            )

    # Always return the event unmodified -- never block Cognito's own flow.
    return event
