"""
SyncService: orchestration logic that sits on top of any UserRepository
implementation.

Why this layer exists, separate from the repository interface
---------------------------------------------------------------
Some behavior in this codebase is a property of HOW syncing should work,
not of any particular storage engine -- it shouldn't have to be
re-implemented correctly by every custom UserRepository someone writes.
The clearest example: audit-log failures must never mask or roll back a
successful user upsert (see docs/architecture.md decision on failure
isolation). That's an orchestration rule, not a SQL detail, so it lives
here once, wrapping whatever repository is configured, rather than
inside PostgresUserRepository where every custom repository would have
to remember to reimplement it correctly.

This is the single entry point the Lambda handlers and reconciler
actually call. They depend on SyncService, which depends on
UserRepository (the interface) -- never on a concrete repository class
directly. Swapping storage is done by constructing SyncService with a
different repository, not by changing any calling code.
"""

import logging

from common.repositories.base import UserRepository

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SyncService:
    def __init__(self, repository: UserRepository):
        self.repository = repository

    def sync_user(self, cognito_sub, email, username, attributes, event_source) -> dict:
        """Upsert a user and record the audit event. The audit write's
        success or failure never affects this method's return value or
        whether it raises -- see module docstring."""
        result = self.repository.upsert_user(
            cognito_sub=cognito_sub, email=email, username=username, attributes=attributes,
        )

        try:
            self.repository.log_sync_event(
                cognito_sub=cognito_sub,
                event_source=event_source,
                status="success",
                detail="insert" if result.get("inserted") else "update",
            )
        except Exception as exc:
            logger.error(
                "User upsert for %s succeeded, but audit log write failed: %s. "
                "This event will not appear in the audit trail.",
                cognito_sub,
                exc,
            )

        return result

    def get_all_users(self) -> list[dict]:
        return self.repository.get_all_users()

    def enqueue_dead_letter(self, cognito_sub, payload, error) -> None:
        self.repository.enqueue_dead_letter(cognito_sub=cognito_sub, payload=payload, error=str(error))

    def log_failure(self, cognito_sub, event_source, detail) -> None:
        self.repository.log_sync_event(
            cognito_sub=cognito_sub, event_source=event_source, status="failure", detail=detail,
        )

    def fetch_unreplayed_dead_letters(self, max_retry: int) -> list[dict]:
        return self.repository.fetch_unreplayed_dead_letters(max_retry)

    def fetch_stuck_dead_letters(self, max_retry: int) -> list[dict]:
        return self.repository.fetch_stuck_dead_letters(max_retry)

    def mark_dead_letter_replayed(self, dead_letter_id) -> None:
        self.repository.mark_dead_letter_replayed(dead_letter_id)

    def record_dead_letter_failure(self, dead_letter_id, error) -> None:
        self.repository.record_dead_letter_failure(dead_letter_id, str(error))
