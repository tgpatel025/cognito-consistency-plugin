"""
Cognito Post Confirmation trigger -- fires once after sign-up (or
forgot-password) confirmation; creates the app-side user record.

Failure handling: Cognito calls this synchronously, and raising would
block the user's sign-up. So we never raise -- on failure we dead-letter
the event for replay, log a 'failure' audit event, and return the event
unmodified. A DB outage never blocks sign-up; it creates drift, which
is what the reconciler is for.

Storage via SyncService, never a specific database. No default backend:
implement UserRepository + set REPOSITORY_CLASS (see
docs/extending-the-repository.md; examples/postgres is the reference).
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
