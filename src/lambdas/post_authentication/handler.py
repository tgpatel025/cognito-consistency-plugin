"""
Cognito Post Authentication trigger.

Fires on every successful sign-in. Used here to catch attribute drift
that happens *between* sign-ins -- e.g. a user's email or name changed
in Cognito (via admin console, another client, or a federated IdP
attribute refresh) but the app DB was never told.

Same failure-handling philosophy as post_confirmation: never block the
auth flow, always leave a trail if the sync fails.
"""

import logging

from common.db import upsert_user, enqueue_dead_letter, log_sync_event

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    if event.get("triggerSource") != "PostAuthentication_Authentication":
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
            event_source="post_authentication",
        )
    except Exception as exc:
        logger.error("Failed to sync user %s on sign-in: %s", cognito_sub, exc)
        # See post_confirmation/handler.py for why this inner try/except
        # exists: these are DB calls too, so they can fail for the same
        # reason upsert_user just did. They must never be allowed to
        # propagate and block the user's sign-in.
        try:
            enqueue_dead_letter(cognito_sub=cognito_sub, payload=attributes, error=exc)
            log_sync_event(
                cognito_sub=cognito_sub,
                event_source="post_authentication",
                status="failure",
                detail=str(exc),
            )
        except Exception as inner_exc:
            logger.critical(
                "Failed to record dead-letter/audit for user %s after sync failure: %s. "
                "This event is now unrecoverable except via Cognito's own user record.",
                cognito_sub,
                inner_exc,
            )

    return event
