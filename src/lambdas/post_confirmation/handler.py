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
there is no default; you must implement UserRepository and point the
REPOSITORY_CLASS env var at it (examples/postgres/repository.py is a
ready-to-use reference implementation, not a default). See
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

    _sync_service.sync_or_dead_letter(
        cognito_sub=cognito_sub,
        email=email,
        username=username,
        attributes=attributes,
        event_source="post_confirmation",
    )

    # Always return the event unmodified -- never block Cognito's own flow.
    return event
