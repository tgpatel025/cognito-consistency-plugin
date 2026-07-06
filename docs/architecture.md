# Architecture

## The problem this demonstrates

Apps using Amazon Cognito for auth commonly keep a second copy of user data in Postgres/MySQL for
business logic. Cognito doesn't support relational queries or joins, so the split is inherent to the
platform.

The split creates drift: failed Lambda invocations, retries, DB outages, schema mismatches, or
attribute edits made directly in Cognito (e.g. admin console) that never reach the app DB.

This project is a small, real implementation of the sync + reconciliation pattern most teams build
ad hoc.

## Data flow

```
                     ┌─────────────────────┐
   User signs up  →  │   Cognito User Pool  │
                     └──────────┬───────────┘
                                │ Post Confirmation trigger
                                ▼
                     ┌─────────────────────┐
                     │ post_confirmation    │──── on failure ───┐
                     │ Lambda               │                   │
                     └──────────┬───────────┘                   ▼
                                │ upsert                 sync_dead_letters
                                ▼                          (Postgres table)
                     ┌─────────────────────┐                   │
                     │   app_users table    │◄──── replay ──────┘
                     │   (Postgres)          │
                     └──────────▲───────────┘
                                │ upsert
                     ┌──────────┴───────────┐
                     │ post_authentication   │
                     │ Lambda                │
                     └──────────▲───────────┘
                                │ Post Authentication trigger
                     ┌──────────┴───────────┐
   User signs in  →  │   Cognito User Pool   │
                     └───────────────────────┘

   Independently, on a schedule (EventBridge, every 15 min):

   ┌───────────────────┐        ┌───────────────────┐
   │  Cognito           │        │  app_users table   │
   │  list_users()      │        │  (Postgres)         │
   └─────────┬──────────┘        └─────────┬──────────┘
             │                              │
             └──────────────┬───────────────┘
                             ▼
                  ┌────────────────────┐
                  │  Reconciler          │
                  │  (drift detection)   │
                  └──────────┬───────────┘
                             ▼
                  ┌────────────────────┐
                  │  sync_audit_log      │  ← queryable compliance trail
                  │  drift report         │
                  └────────────────────┘
```

## Key design decisions (and why)

### 1. Never block the Cognito flow on a DB write failure

Cognito's Lambda triggers are synchronous (5-second timeout); a throwing trigger fails the user's
sign-up or sign-in — worse than the sync problem itself. Both handlers call
`SyncService.sync_or_dead_letter()`, which catches all exceptions, writes a `sync_dead_letters` row,
and never propagates the failure. Handlers return the event unmodified, so Cognito's flow always
succeeds. See `src/common/sync_service.py` and `src/lambdas/*/handler.py`.

**Trade-off**: a DB outage produces *drift*, not a stuck sign-up. Correct for identity — but the
reconciler must be trusted to catch and fix that drift later.

### 2. Detection and remediation are separate operations

`reconciler/drift.py`'s `find_drift()` is a pure function: two lists in, typed drift records out, no
writes. Applying fixes is a distinct, explicit step (`reconciler/run.py --fix`, or
`reconciler/replay.py` for dead letters).

**Why**: a tool that silently repairs on every scheduled run can silently overwrite data in the
wrong direction if the diff logic has a bug. Separating "what's wrong" from "fix it" lets an
operator review the diff first — same reasoning as Terraform's plan/apply split.

### 3. Cognito is the source of truth for identity attributes; orphans are never auto-deleted

On email/username mismatch, the reconciler overwrites the DB with Cognito's value — Cognito owns
identity. But a DB row with no matching Cognito user (`ORPHANED_IN_DB`) is only flagged: deleting
rows that may be referenced by other business data (orders, permissions, audit history) is far
higher-risk than fixing a stale email, so it's left to a human.

### 4. Everything writes an audit trail, on both success and failure

The audit trail is append-only and records every sync attempt — the piece regulated environments
(HealthTech, FinTech, GovTech) actually care about. See `UserRepository.log_sync_event` (decision #9
covers why it's an interface method, not a fixed table).

### 5. Idempotency via upsert on `cognito_sub`

Cognito's retries plus the replay path mean the same event can be processed more than once. All
writes key on the immutable `cognito_sub`, so re-processing is a no-op, not a duplicate row.

**The audit write is separate from, and cannot affect, the primary write**:
`SyncService.sync_user()` (`common/sync_service.py`) calls `upsert_user()` and then, separately,
`log_sync_event()` — independent operations, potentially independent transactions. Intentional: the
audit log is a record *about* the user record, and a less-critical record must never veto a
more-critical write. One shared transaction would let a full audit-table disk or a misbehaving trigger
roll back a successful user sync — exactly backwards.

Corollary, enforced explicitly in `SyncService`: if the audit write fails after a successful upsert,
`sync_user()` still returns success. The failure is logged loudly (compliance gap visible in
CloudWatch) but never propagates — otherwise a handler could enqueue an unnecessary dead letter for
a write that actually succeeded. See `tests/test_sync_service.py` for both cases. This guarantee
lives once, in `SyncService`, not in every repository — see decision #9.

**Remaining gap**: in the shipped Postgres example
(`examples/postgres/repository.py::PostgresUserRepository`), the user write and audit write are two
separate transactions (two `_cursor()` calls). If the Lambda is frozen or killed between them, the
audit trail permanently under-reports, with no automatic detection or backfill. Acceptable residual
risk for an example; a stricter guarantee needs a transactional outbox (audit event written in the
*same* transaction as `app_users`, published separately) or audit events derived from a CDC/WAL
stream. Specific to that example — another `UserRepository` could make it atomic if its engine
supports it.

### 6. Silent failures are alarmable, not just logged

`logger.critical(...)` in the handlers fires only when *both* the primary sync and the
dead-letter/audit fallback fail (decision #1) — an event lost with zero database record. A log line alone
isn't safe: nobody reads logs proactively, and retention expires them.

Two independent alarm paths, defined in
[`infra/terraform/module/alerting.tf`](../infra/terraform/module/alerting.tf):

- **Critical log alarm**: a CloudWatch Logs metric filter scans each sync Lambda's log group for
  `CRITICAL`; alarm on ≥1 occurrence. The "event lost entirely" case — rare, severe, notify
  immediately.
- **Drift accumulation alarm**: the scheduled reconciler (`scheduled_handler.py`) publishes a
  `DriftCount` metric each run, by drift type plus total. Alarm fires if total drift stays at or
  above a threshold across N consecutive runs (configurable via `drift_alarm_threshold` /
  `drift_alarm_evaluation_periods`). Catches the common case: recorded failures accumulating
  faster than they're replayed, or drift from other causes (e.g. a direct Cognito admin edit).

Both publish to a shared SNS topic (`aws_sns_topic.alerts`) — email subscription out of the box,
extendable (Slack via AWS Chatbot, PagerDuty, etc.) without code changes.

**Why two alarms, not one**: different severities, different responses. Critical-log means "go check
why Postgres is unreachable, right now." Drift-accumulation means "review the diff, decide whether
to run `--fix`." One combined alarm is either too noisy to page on or a source of alarm fatigue.

### 7. Dead-letter replay has a retry limit, so bad data can't retry forever

The first `replay.py` retried every unreplayed dead letter unconditionally — no way to tell a
*transient* failure (DB briefly down) from a *permanent* one (invalid payload, e.g. null email vs a
`NOT NULL` constraint). Permanent failures retried identically forever, and the
`N succeeded, M failed` summary couldn't show whether `M` was the same stuck entry every time.

Each dead-letter row now tracks `retry_count` and `last_error`. Entries at or past
`MAX_RETRY_ATTEMPTS` (5) are excluded from normal replay and surfaced via
`python -m reconciler.replay --report`, which prints the `cognito_sub`, retry count, and last error
per stuck entry. An invisible, infinitely-retried failure becomes a visible, bounded one — same triage model
as an SQS DLQ.

**Trade-off**: SQS enforces max-receive-count at the infrastructure level; here it's application
code against a Postgres column. Simpler for a demo, but the retry logic lives in `replay.py`, not
the queue mechanism.

### 8. The Terraform module owns only what this project invented

The first Terraform config provisioned everything — Cognito pool, RDS, Lambdas — in one flat root
module. Wrong shape for adoption: a real team already has a pool (with users that can't be
recreated), a database with their own schema, and their own VPC/security posture. A module that owns
those forces a migration, not an integration.

[`infra/terraform/module`](../infra/terraform/module) takes the User Pool ARN, a Secrets Manager ARN
for the database, and optional VPC config as **inputs**, and creates only the Lambdas, per-function
IAM roles, and the alerting — the actual reusable unit. `lambda_config` (wiring the Lambdas as pool
triggers) is set by the *consumer's* root module, since Terraform's `aws_cognito_user_pool` resource
requires the whole pool in one block this module doesn't own.

**Why no "create everything from scratch" example**: an earlier version had one — module call plus a
new pool in the same root. Genuine circular dependency: the reconciler's IAM policy needs the pool's
ARN (exact-resource scoping, not a wildcard), while the pool's `lambda_config` needs the module's
Lambda ARNs. The cycle doesn't exist with a real existing pool (its ARN is already a known value),
so rather than loosen IAM scoping for a demo path, the path was removed.
[`infra/localstack`](../infra/localstack) is the demo path instead — same sync/reconciliation code,
no real AWS resources.

### 9. The database layer is an interface, not a fixed schema

Earlier versions baked one Postgres schema (`app_users` / `sync_audit_log` / `sync_dead_letters`) as
raw SQL into the core library — the same mistake as decision #8's Terraform. A real adopter has a
`users` table with different columns, a different primary key, maybe a different engine. Requiring
this schema makes it a migration, not a library.

`common/repositories/base.py` defines `UserRepository`, an abstract interface covering exactly the
operations the codebase needs (`upsert_user`, `get_all_users`, `log_sync_event`, and the
dead-letter/replay methods). Every Lambda handler, the reconciler, and replay depend on this interface
via `SyncService` (`common/sync_service.py`) — never on a concrete implementation.

**Where cross-cutting behavior lives**: logic like audit-log failures never masking a successful
upsert (the failure-isolation decision above) is a property of *how syncing should work*, not of any
specific SQL. It lives once in `SyncService`, wrapping whatever repository is configured, not
re-implemented (and potentially gotten wrong) in every custom repository. A custom `UserRepository`
only needs its own storage calls right; the orchestration around them is already correct.

**Trade-off**: adds indirection (interface → factory → concrete implementation) a single-schema
project wouldn't need — an explicit cost for reusability. See
[`docs/extending-the-repository.md`](./extending-the-repository.md).

### 10. No default repository, and examples live outside `src/`

Decision #9 first shipped `PostgresUserRepository` as a *default* (used when `REPOSITORY_CLASS` was
unset, so a fresh clone ran with zero config). A default is still an opinion, with a one-sided cost:
every deployment carried `psycopg2-binary` (a compiled binary dependency) in core `requirements.txt`
and every Lambda bundle, Postgres or not. And `common/db.py`, despite its "schema-independent"
docstring, imported `psycopg2` at module level — never *engine*-independent. Owning a default
database driver is exactly the opinion the interface was built to avoid.

**What changed**:
- `common/service_factory.py::build_sync_service()` now **requires** `REPOSITORY_CLASS`. Unset
  raises `RuntimeError` at Lambda cold-start (module import time), not silently on first
  invocation, with a message pointing at the docs and the shipped example. Misconfigured
  deployments fail loudly and early, not accept traffic they can't sync.
- The factory constructs your class with **zero arguments**: `repository_class()`. No imposed
  `connect_fn` convention — connection is the repository's own `__init__`'s business. This also
  removed the factory's `TypeError`-catching fallback logic.
- `common/db.py` is deleted from core; its logic moved to `examples/postgres/connection.py`, owned
  by that example.
- The repositories from `src/common/repositories/` (`postgres.py`, `example_custom_schema.py`)
  moved to a top-level [`examples/`](../examples) directory (`examples/postgres/repository.py`,
  `examples/custom_schema_partial/repository.py`), each with their own `requirements.txt` where
  relevant. `src/common/repositories/` now contains only `base.py`.
- Core `requirements.txt` has zero database driver dependencies.

**Why examples live outside `src/`**: mixing runnable examples into the library's directories makes
it unclear which files are the library and which are copy-and-modify starting points. A separate
top-level directory makes that distinction structural.

**Cost**: no longer "clone and run" — even the LocalStack demo requires setting `REPOSITORY_CLASS`
and installing `examples/postgres/requirements.txt` (see `docs/local-demo.md`). Accepted: a slightly
less immediate demo, in exchange for a core library that never carries an opinion — or a dependency
— it didn't ask for.

## What's out of scope for this demo (and why)

- **Multi-tenancy** — a real product would need per-tenant isolation, which changes the schema and
  the `--fix` blast radius significantly.
- **Field-level conflict resolution** — the mismatch-repair logic treats Cognito as authoritative
  for *all* compared fields. A more mature version might need per-field ownership rules (e.g. app
  DB owns a `display_name` override that shouldn't be clobbered by Cognito's `name` attribute).
