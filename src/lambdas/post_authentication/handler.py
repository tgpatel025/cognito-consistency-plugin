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

    _sync_service.sync_or_dead_letter(
        cognito_sub=cognito_sub,
        email=email,
        username=username,
        attributes=attributes,
        event_source="post_authentication",
    )

    return event
