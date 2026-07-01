"""
Cognito Post Authentication trigger.

Fires on every successful sign-in. Used here to catch attribute drift
that happens *between* sign-ins -- e.g. a user's email or name changed
in Cognito (via admin console, another client, or a federated IdP
attribute refresh) but the app DB was never told.

Same failure-handling philosophy as post_confirmation: never block the
auth flow, always leave a trail if the sync fails.

Storage: depends on SyncService, not on any specific database directly
-- see post_confirmation/handler.py's docstring and
docs/extending-the-repository.md for how to point this at your own
schema.
"""

import logging

from common.service_factory import build_sync_service

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sync_service = build_sync_service()


def handler(event, context):
    if event.get("triggerSource") != "PostAuthentication_Authentication":
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
            event_source="post_authentication",
        )
    except Exception as exc:
        logger.error("Failed to sync user %s on sign-in: %s", cognito_sub, exc)
        # See post_confirmation/handler.py for why this inner try/except
        # exists: these go through the same repository, so they can fail
        # for the same reason sync_user just did. They must never be
        # allowed to propagate and block the user's sign-in.
        try:
            _sync_service.enqueue_dead_letter(cognito_sub=cognito_sub, payload=attributes, error=exc)
            _sync_service.log_failure(
                cognito_sub=cognito_sub, event_source="post_authentication", detail=str(exc),
            )
        except Exception as inner_exc:
            logger.critical(
                "Failed to record dead-letter/audit for user %s after sync failure: %s. "
                "This event is now unrecoverable except via Cognito's own user record.",
                cognito_sub,
                inner_exc,
            )

    return event
