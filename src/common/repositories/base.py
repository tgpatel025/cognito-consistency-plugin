"""
UserRepository: the storage contract everything else depends on --
never on Postgres or any specific table shape.

Handlers, reconciler, and replay only ever call these methods, so you
can point this project at your existing users table (any name, columns,
or engine: MySQL, DynamoDB, whatever) by implementing this interface.
examples/postgres/repository.py is the reference implementation; see
docs/extending-the-repository.md for the guide.

Return shapes
-------------
User records (implementation maps its own columns to these keys):
    {"cognito_sub": str, "email": str | None, "username": str | None,
     "attributes": dict, "last_synced_at": datetime}

Dead-letter records:
    {"id": Any, "cognito_sub": str, "payload": dict, "retry_count": int,
     "last_error": str | None, "occurred_at": datetime}

"id" is Any on purpose: an opaque handle the repository hands back and
later accepts (mark_replayed(id)). Nothing else inspects it -- int,
UUID, composite key, whatever your store uses.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class UserRepository(ABC):
    """Implement this against your own database/schema. See
    examples/postgres/repository.py for the reference implementation and
    docs/extending-the-repository.md for a guide to writing your own."""

    # -- Primary sync path -------------------------------------------------

    @abstractmethod
    def upsert_user(
        self,
        cognito_sub: str,
        email: Optional[str],
        username: Optional[str],
        attributes: dict,
    ) -> dict:
        """Create or update the record for this cognito_sub.

        Must be idempotent -- same sub + data twice must not duplicate or
        raise; Cognito retries and replay both depend on it.

        Returns at least {"id": <opaque id>, "inserted": bool} ("inserted"
        distinguishes new vs update, for audit detail).
        """
        raise NotImplementedError

    @abstractmethod
    def get_all_users(self) -> list[dict]:
        """Return every synced user record (shape: see module docstring).
        Used by the reconciler to diff against Cognito. Paginate
        internally if you must -- the contract is the full set comes back."""
        raise NotImplementedError

    # -- Audit trail ---------------------------------------------------

    @abstractmethod
    def log_sync_event(
        self,
        cognito_sub: str,
        event_source: str,
        status: str,
        detail: Optional[str] = None,
    ) -> None:
        """Append-only audit record -- never update/delete rows; it's the
        compliance trail. SyncService treats exceptions here as non-fatal:
        an audit failure must never mask a successful upsert_user."""
        raise NotImplementedError

    # -- Dead-letter / replay -------------------------------------------

    @abstractmethod
    def enqueue_dead_letter(self, cognito_sub: str, payload: dict, error: str) -> None:
        """Record a failed sync for later replay. `payload` is opaque --
        store and return it unchanged. (SyncService writes
        {"username": ..., "attributes": ...}; replay.py reads it back.)"""
        raise NotImplementedError

    @abstractmethod
    def fetch_unreplayed_dead_letters(self, max_retry: int) -> list[dict]:
        """Return dead letters eligible for replay: not yet replayed,
        and with retry_count < max_retry. Entries at or past max_retry
        are excluded -- see fetch_stuck_dead_letters for those."""
        raise NotImplementedError

    @abstractmethod
    def fetch_stuck_dead_letters(self, max_retry: int) -> list[dict]:
        """Return dead letters that have exceeded max_retry attempts
        and are not being automatically retried -- these need manual
        investigation."""
        raise NotImplementedError

    @abstractmethod
    def mark_dead_letter_replayed(self, dead_letter_id: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def record_dead_letter_failure(self, dead_letter_id: Any, error: str) -> None:
        """Increment retry_count and record the latest error for a
        dead letter that failed to replay."""
        raise NotImplementedError
