# Extending the repository: using your own database schema

This project ships with a working Postgres schema
([`infra/localstack/schema.sql`](../infra/localstack/schema.sql):
`app_users`, `sync_audit_log`, `sync_dead_letters`), but it does not
require you to adopt it. Every Lambda handler, the reconciler, and
replay depend on an interface —
[`UserRepository`](../src/common/repositories/base.py) — not on that
specific schema or even on Postgres. If you already have a `users`
table with different columns, a different primary key, or a different
database engine entirely, you write your own implementation of that
interface and nothing else in this codebase changes.

## Why this exists

The Terraform side of this project went through the same realization
(see [`docs/architecture.md`](./architecture.md) decision #8): a real
adopter already has their own Cognito pool and their own database. A
project that insists on its own exact schema is no more adoptable than
one that insists on creating its own Cognito pool — it's a migration,
not an integration. The repository interface applies the same principle
one layer down, to the data model instead of the infrastructure.

## The interface

See [`src/common/repositories/base.py`](../src/common/repositories/base.py)
for the full, documented contract. In short, you implement:

| Method | Purpose |
|---|---|
| `upsert_user(cognito_sub, email, username, attributes)` | Create or update a user record. Must be idempotent. |
| `get_all_users()` | Return every synced user, for the reconciler to diff against Cognito. |
| `log_sync_event(cognito_sub, event_source, status, detail)` | Append an audit record. |
| `enqueue_dead_letter(cognito_sub, payload, error)` | Record a failed sync for later replay. |
| `fetch_unreplayed_dead_letters(max_retry)` | Dead letters eligible for retry. |
| `fetch_stuck_dead_letters(max_retry)` | Dead letters that exceeded the retry limit. |
| `mark_dead_letter_replayed(id)` | Mark a dead letter successfully replayed. |
| `record_dead_letter_failure(id, error)` | Increment retry count after a failed replay. |

Return shapes (dict keys expected by the rest of the codebase) are
documented in the interface's docstring.

## Worked example

[`src/common/repositories/example_custom_schema.py`](../src/common/repositories/example_custom_schema.py)
implements the interface against a deliberately different, realistic
pre-existing schema: an integer-PK `users` table with a nullable
`cognito_id` column (not `cognito_sub` as the primary key), a generic
`event_log` table reused for audit events instead of a dedicated
`sync_audit_log`, and a generic `failed_jobs` table reused for dead
letters. It's meant to be copied and adapted — your real schema will
differ from the one imagined there, but it demonstrates the *shape* of
mapping your own columns to the interface's expected keys.

## Wiring it in

1. Write your implementation, subclassing `UserRepository` and
   implementing every abstract method (Python's `ABC` machinery will
   raise `TypeError` at instantiation if you miss one — see
   `tests/test_repository_interface.py` for how this is verified).
2. Set the `REPOSITORY_CLASS` environment variable to
   `"your_module.path:YourClassName"` (or, via Terraform, set the
   module's `repository_class` variable — see
   [`infra/terraform/module/README.md`](../infra/terraform/module/README.md)).
3. Bundle your module into the Lambda deployment package alongside
   `src/`, the same way `psycopg2`/`boto3` are vendored today (see
   [`scripts/build_lambda_deps.sh`](../scripts/build_lambda_deps.sh)).
4. Nothing else changes. The Lambda handlers, reconciler, and replay
   logic all depend on `SyncService` (`src/common/sync_service.py`),
   which depends on `UserRepository`, never on `PostgresUserRepository`
   directly. See `src/common/service_factory.py` for exactly how the
   class gets loaded.

## Constructor signature

`build_sync_service()` tries to construct your class with a single
argument (`connect_fn`, matching `PostgresUserRepository`'s signature —
useful if you're also using Postgres/MySQL via a `connect()`-style
function). If that raises `TypeError`, it falls back to a no-argument
constructor, for implementations that manage their own connection setup
(e.g. a DynamoDB repository using boto3's default credential chain, or
one that reads its own connection details from different env vars).

## What you get for free by implementing the interface

Every cross-cutting behavior already built into this project — audit-log
failures never masking a successful sync (`SyncService`), the two-alarm
CloudWatch alerting, the dead-letter retry-limit / poison-pill handling —
works against your schema automatically, because it's implemented once,
against the interface, not duplicated per-repository. You only need to
get your SQL (or API calls, if not using SQL at all) right; the
orchestration logic around it is already correct.
