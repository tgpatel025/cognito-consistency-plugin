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

from common.db import upsert_user, enqueue_dead_letter

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
        enqueue_dead_letter(cognito_sub=cognito_sub, payload=attributes, error=exc)

    return event
