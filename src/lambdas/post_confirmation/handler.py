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
"""

import logging

from common.db import upsert_user, enqueue_dead_letter

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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
        upsert_user(
            cognito_sub=cognito_sub,
            email=email,
            username=username,
            attributes=attributes,
            event_source="post_confirmation",
        )
        logger.info("Synced user %s to app database", cognito_sub)
    except Exception as exc:
        logger.error("Failed to sync user %s: %s", cognito_sub, exc)
        enqueue_dead_letter(cognito_sub=cognito_sub, payload=attributes, error=exc)

    # Always return the event unmodified -- never block Cognito's own flow.
    return event
