"""
SyncService: orchestration on top of any UserRepository.

Rules about HOW syncing works (e.g. an audit-log failure must never
mask a successful upsert -- see docs/architecture.md) live here once,
instead of being re-implemented by every custom repository. Handlers
and the reconciler call this, never a concrete repository -- swap
storage by constructing SyncService with a different repository.
"""

import logging

from common.repositories.base import UserRepository

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SyncService:
    def __init__(self, repository: UserRepository):
        self.repository = repository

    def sync_user(self, cognito_sub, email, username, attributes, event_source) -> dict:
        """Upsert a user + record the audit event. The audit write never
        affects the return value or whether this raises."""
        result = self.repository.upsert_user(
            cognito_sub=cognito_sub, email=email, username=username, attributes=attributes,
        )
        detail = "insert" if result.get("inserted") else "update"

        try:
            self.repository.log_sync_event(
                cognito_sub=cognito_sub,
                event_source=event_source,
                status="success",
                detail=detail,
            )
        except Exception as exc:
            # Exception type only -- str(exc) from DB drivers can embed
            # row values (PII), and CloudWatch is wider-access than the DB.
            logger.error(
                "User upsert for %s succeeded, but audit log write failed (%s). "
                "This event will not appear in the audit trail.",
                cognito_sub,
                type(exc).__name__,
            )

        return result

    def sync_or_dead_letter(self, cognito_sub, email, username, attributes, event_source) -> bool:
        """Sync a user; on failure, dead-letter it instead of raising.
        For callers that must never propagate a sync failure (the Lambda
        triggers). Returns True on success, False on failure.

        Payload keeps username alongside attributes -- username isn't a
        Cognito attribute, and replay needs it to rebuild the sync_user
        call."""
        try:
            self.sync_user(
                cognito_sub=cognito_sub,
                email=email,
                username=username,
                attributes=attributes,
                event_source=event_source,
            )
            return True
        except Exception as exc:
            # Type only here; full error text lands in the dead-letter row
            # below, the narrower-access sink for that detail.
            logger.error("Failed to sync user %s via %s (%s)", cognito_sub, event_source, type(exc).__name__)
            try:
                self.enqueue_dead_letter(
                    cognito_sub=cognito_sub,
                    payload={"username": username, "attributes": attributes},
                    error=exc,
                )
                self.log_failure(cognito_sub=cognito_sub, event_source=event_source, detail=str(exc))
            except Exception as inner_exc:
                logger.critical(
                    "Failed to record dead-letter/audit for user %s after sync failure: %s. "
                    "This event is now unrecoverable except via Cognito's own user record.",
                    cognito_sub,
                    inner_exc,
                )
            return False

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
