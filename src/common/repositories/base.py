"""
UserRepository: the contract this project depends on, independent of
any specific database engine or table schema.

Why this exists
----------------
Every other module in this codebase (the Lambda handlers, the
reconciler, replay) needs to do the same handful of things: upsert a
user record, log a sync event, list all synced users, and manage
dead letters. Originally these were raw SQL statements against a fixed
`app_users` / `sync_audit_log` / `sync_dead_letters` Postgres schema
baked directly into the core library.

That's fine for a demo, but wrong for something meant to be dropped into
someone else's existing system: a real adopter already has a `users`
table, with their own name, columns, primary key, and possibly a
different database engine entirely (MySQL, DynamoDB, whatever). Forcing
them to adopt this project's exact schema is the same mistake the
Terraform module used to make by trying to create its own Cognito pool
and RDS instance -- see docs/architecture.md decision #8.

The fix, applied the same way: everything else in this codebase depends
on THIS interface, not on Postgres or on any specific table shape.
PostgresUserRepository (repositories/postgres.py) is the reference
implementation, matching the schema in infra/localstack/schema.sql --
useful to run as-is, or to copy and adapt. A real adopter writes their
own implementation of this same interface against their existing
table(s) and engine, and every Lambda handler / the reconciler / replay
keeps working unmodified, because they only ever call methods on
UserRepository, never raw SQL.

Return shapes
-------------
Methods that return user records return plain dicts with these keys,
regardless of the underlying schema's actual column names -- the
repository implementation is responsible for mapping its own columns to
these names:
    {"cognito_sub": str, "email": str | None, "username": str | None,
     "attributes": dict, "last_synced_at": datetime}

Dead-letter records use:
    {"id": Any, "cognito_sub": str, "payload": dict, "retry_count": int,
     "last_error": str | None, "occurred_at": datetime}

"id" is intentionally typed as Any -- it's an opaque handle the
repository hands back and later accepts (e.g. mark_replayed(id)); the
rest of the codebase never inspects or constructs it, so it can be an
int, a UUID, a composite key, whatever the underlying store uses.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class UserRepository(ABC):
    """Implement this against your own database/schema. See
    repositories/postgres.py for the reference implementation and
    repositories/README.md (docs/extending-the-repository.md at the
    repo root) for a guide to writing your own."""

    # -- Primary sync path -------------------------------------------------

    @abstractmethod
    def upsert_user(
        self,
        cognito_sub: str,
        email: Optional[str],
        username: Optional[str],
        attributes: dict,
    ) -> dict:
        """Create or update the user record for this cognito_sub.

        Must be idempotent: calling this twice with the same cognito_sub
        and data must not create a duplicate record or raise -- Cognito's
        own retry behavior and the reconciler's replay path both depend
        on this.

        Returns a dict with at least {"id": <opaque id>, "inserted": bool}
        -- "inserted" distinguishes a new record from an update, used
        for audit logging detail.
        """
        raise NotImplementedError

    @abstractmethod
    def get_all_users(self) -> list[dict]:
        """Return every synced user record, in the shape described in
        this module's docstring. Used by the reconciler to diff against
        Cognito's user list -- for very large user pools, an
        implementation may want to paginate internally, but this
        interface's contract is that the full set is returned."""
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
        """Append-only audit record. Implementations should make this
        append-only (never update/delete existing rows) since it's the
        compliance trail. Callers (specifically SyncService, see
        common/sync_service.py) treat a raised exception here as
        recoverable/non-fatal -- the audit write must never be allowed
        to mask or roll back a successful upsert_user call."""
        raise NotImplementedError

    # -- Dead-letter / replay -------------------------------------------

    @abstractmethod
    def enqueue_dead_letter(self, cognito_sub: str, payload: dict, error: str) -> None:
        """Record a failed sync attempt for later replay. `payload` is
        opaque to the repository -- store and return it unchanged.
        Callers (SyncService.sync_or_dead_letter) write it as
        {"username": str | None, "attributes": dict}, and replay.py reads
        it back in that same shape to reconstruct the sync_user() call."""
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
