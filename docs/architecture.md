# Architecture

## The problem this demonstrates

Applications that use Amazon Cognito for authentication commonly keep a
second copy of user data in Postgres/MySQL for business logic — profiles,
authorization, reporting, relationships to other domain data. Cognito
doesn't support arbitrary relational queries or joins, so this split is
inherent to the platform, not a mistake.

The split creates a synchronization problem: Cognito and the application
database can drift apart due to failed Lambda invocations, retries,
database outages, schema mismatches, or attribute changes made directly
in Cognito (e.g. via the admin console) that never reach the app DB.

This project is a small, real implementation of the sync + reconciliation
pattern that most teams build ad hoc. See [`docs/market-context.md`](./market-context.md)
for an honest discussion of how common and how severe this problem
actually is — the short version: the *pattern* is real and well-documented,
but the *pain* is usually low-severity, which is why no dedicated
commercial product exists for it. That's precisely why this makes a good
portfolio piece: it's a well-scoped systems problem, not a padded pitch.

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

Cognito's Lambda triggers are synchronous with a 5-second timeout. If a
trigger throws, the user's sign-up or sign-in fails — a UX and
availability regression far worse than the sync problem itself. Both
Lambda handlers deliberately catch all exceptions, write a
`sync_dead_letters` row, and return the event unmodified so Cognito's own
flow always succeeds. See `src/lambdas/*/handler.py`.

**Trade-off**: this means a DB outage produces *drift*, not a stuck
sign-up. That's the correct trade-off for identity, but it does mean the
reconciler must be trusted to catch and fix that drift later.

### 2. Detection and remediation are separate operations

`reconciler/drift.py`'s `find_drift()` is a pure function: two lists in,
a list of typed drift records out. It never writes anywhere. Applying
fixes is a distinct, explicit step (`reconciler/run.py --fix`, or
`reconciler/replay.py` for dead letters).

**Why this matters**: a reconciliation tool that both detects and
silently repairs on every scheduled run is a tool that can silently
overwrite data in the wrong direction if a bug creeps into the diff
logic. Separating "what's wrong" from "fix it" means an operator (or a
future approval workflow) can review the diff before it's applied — this
is the same reasoning behind Terraform's plan/apply split.

### 3. Cognito is the source of truth for identity attributes; orphans are never auto-deleted

When email/username mismatch, the reconciler overwrites the DB with
Cognito's value — Cognito owns identity. But when a DB row has no
matching Cognito user (`ORPHANED_IN_DB`), the reconciler only flags it.
Automatically deleting rows that may be referenced by other business
data (orders, permissions, audit history) is a much higher-risk action
than correcting a stale email address, and is left to a human decision.

### 4. Everything writes an audit trail, on both success and failure

The audit trail is append-only and records every sync attempt — this is
the "auditability" piece that regulated environments (HealthTech,
FinTech, GovTech) actually care about, more so than the sync mechanism
itself. See `UserRepository.log_sync_event` (decision #9, below, covers
why this is an interface method rather than a fixed table).

### 5. Idempotency via upsert on `cognito_sub`

Cognito's own retry behavior, plus the reconciler's replay path, mean the
same event can be processed more than once. All writes key on the
immutable `cognito_sub`, so re-processing an event is a no-op change, not
a duplicate row.

**Why the audit write is separate from, and cannot affect, the primary
write**: `SyncService.sync_user()` (`common/sync_service.py`) calls the
repository's `upsert_user()` and then, separately, `log_sync_event()`.
These are independent operations, potentially independent transactions
depending on the repository implementation. This is intentional, not an
oversight: the audit log is a record *about* the user record, and a
less-critical record should never be able to veto a more-critical write.
Wrapping both in one shared transaction would mean a full audit-table
disk, a misbehaving trigger, or any other audit-side problem could roll
back a successful user sync — exactly backwards from what you want.

The corollary, which `SyncService` enforces explicitly: if the audit
write fails after the upsert has already succeeded, `sync_user()` still
returns success and does not raise. The failure is logged loudly (so the
compliance gap is visible in CloudWatch), but it's not allowed to
propagate back to the caller and be mistaken for a sync failure — that
would risk a Lambda handler enqueueing an unnecessary dead letter for a
write that actually succeeded. See `tests/test_sync_service.py` for the
explicit case (audit write fails, primary write is still reported as
successful) and its counterpart (primary write fails, audit log is never
even attempted).

This guarantee is enforced once, in `SyncService`, rather than inside
every repository implementation — see decision #9 for why that
placement matters.

**Remaining gap**: in `PostgresUserRepository`, the user-record write and
the audit-log write are two separate transactions (two separate
`db_cursor()` calls). There's a narrow window — the Lambda being frozen
or killed between the two calls — where the audit trail permanently
under-reports relative to the user table, with no automatic way to
detect or backfill it. This is judged an acceptable residual risk for a
demo (a single function call is a small window), but a stricter guarantee would
need either a transactional outbox (write the intended audit event in
the *same* transaction as `app_users`, as a row in an outbox table, then
have a separate process publish it to `sync_audit_log`) or deriving audit
events from a CDC/WAL stream on `app_users` instead of writing them from
application code at all.

### 6. Silent failures are alarmable, not just logged

`logger.critical(...)` in the Lambda handlers only fires when *both* the
primary sync and the dead-letter/audit fallback fail (see decision #1
above) — meaning an event is lost with zero database record of it. A log
line alone is not a safe place to leave that: nobody reads logs
proactively, and log retention eventually expires it entirely.

Two independent alarm paths cover this, defined in
[`infra/terraform/module/alerting.tf`](../infra/terraform/module/alerting.tf):

- **Critical log alarm**: a CloudWatch Logs metric filter scans each
  sync Lambda's log group for `CRITICAL` and fires an alarm on ≥1
  occurrence. This is the "an event was lost entirely" case — fast,
  rare, and severe enough to notify on immediately.
- **Drift accumulation alarm**: the scheduled reconciler
  (`scheduled_handler.py`) publishes a `DriftCount` CloudWatch metric on
  every run, broken down by drift type plus a total. An alarm fires if
  total drift stays at or above a threshold across N consecutive runs
  (both configurable via `drift_alarm_threshold` /
  `drift_alarm_evaluation_periods`). This catches the more common case:
  individual sync failures that *were* recorded but are accumulating
  faster than they're replayed, or drift from causes other than sync
  failures (e.g. a direct Cognito admin edit).

Both alarms publish to a shared SNS topic (`aws_sns_topic.alerts`), which
supports an email subscription out of the box and can be extended with
additional subscribers (Slack via AWS Chatbot, PagerDuty, etc.) without
code changes.

**Why two separate alarms instead of one**: they represent different
severities and different response actions. A critical-log alert means
"go check why Postgres is unreachable, right now." A drift-accumulation
alert means "review the diff and decide whether to run `--fix`" — a much
less urgent, more deliberate action. Collapsing them into one alarm would
either make the urgent case too noisy to page on, or the routine case too
alarming to ignore fatigue.

### 7. Dead-letter replay has a retry limit, so bad data can't retry forever

The first version of `replay.py` retried every unreplayed dead letter
unconditionally, with no way to tell a *transient* failure (DB was
briefly down, replaying now succeeds) from a *permanent* one (the
payload itself is invalid — e.g. a null email hitting a `NOT NULL`
constraint). A permanent failure would fail identically on every replay
run, forever, and the summary output (`N succeeded, M failed`) gave no
way to see whether `M` was the same stuck entry every time or a new
failure each time.

Each dead-letter row now tracks `retry_count` and `last_error`. Entries
at or past `MAX_RETRY_ATTEMPTS` (5) are excluded from normal replay and
surfaced separately via `python -m reconciler.replay --report`, which
prints the specific `cognito_sub`, retry count, and last error for each
stuck entry. This turns an invisible, infinitely-retried failure into a
visible, bounded one that a human can act on — similar to how you'd
triage a dead-letter queue in SQS rather than assuming "retry" is always
the right default action.

**Trade-off**: this pattern-matches SQS DLQ semantics (which also have a
max-receive-count before an event is considered undeliverable), but SQS
enforces it at the infrastructure level; here it's enforced in
application code against a Postgres column. That's simpler to build for
a demo, but means the retry-count logic lives in `replay.py` rather than
being a property of the underlying queue mechanism.

### 8. The Terraform module owns only what this project invented

The first version of the Terraform config provisioned everything from
scratch: a Cognito User Pool, an RDS instance, and the sync/reconciler
Lambdas, all in one flat root module. That's the wrong shape for
something meant to be *adopted*: a real developer already has a Cognito
pool (with real users in it — it can't be recreated), an existing
database with their own schema, and their own VPC/security posture. A
module that tries to own those forces a migration, not an integration.

[`infra/terraform/module`](../infra/terraform/module) now takes the
User Pool ARN, a Secrets Manager ARN for the database, and optional VPC
config as **inputs**, and creates only the Lambdas, their per-function
IAM roles, and the alerting that watches them — the actual reusable
unit. `lambda_config` (wiring the Lambdas as the pool's triggers) is set
by the *consumer's* root module against their own pool resource, not
inside this module, since Terraform's `aws_cognito_user_pool` resource
requires the whole pool to be declared in one block and this module
doesn't own that block.

**Why there's no "create everything from scratch" example**: an earlier
version had one, calling the module while also creating a brand-new
pool in the same root. That produces a genuine circular dependency —
the reconciler's IAM policy needs the pool's ARN (exact-resource
scoping, not a wildcard), while the pool's `lambda_config` needs the
module's Lambda ARNs. This cycle doesn't exist in the real integration
case (an existing pool's ARN is already a known value, not something
being created in the same apply), so rather than loosen IAM scoping
just to make a from-scratch demo path work, that path was removed.
[`infra/localstack`](../infra/localstack) is the demo path instead —
it exercises the same sync/reconciliation code without needing any real
AWS resources, let alone ones this module would have to help create.

### 9. The database layer is an interface, not a fixed schema

Everything up to this point still assumed one specific Postgres schema
(`app_users` / `sync_audit_log` / `sync_dead_letters`) baked directly
into raw SQL inside `common/db.py`. That's the same mistake the
Terraform module used to make with its own Cognito pool and RDS
instance (decision #8) — a real adopter already has a `users` table,
almost certainly with different columns, a different primary key, and
possibly a different database engine entirely. Requiring them to adopt
this project's exact schema would make it a migration, not a library.

The fix follows the same shape: `common/repositories/base.py` defines
`UserRepository`, an abstract interface covering exactly the operations
the rest of the codebase needs (`upsert_user`, `get_all_users`,
`log_sync_event`, and the dead-letter/replay methods). Every Lambda
handler, the reconciler, and replay now depend on this interface via
`SyncService` (`common/sync_service.py`) — never on
`PostgresUserRepository` or any specific schema directly.

`PostgresUserRepository` (`common/repositories/postgres.py`) is the
reference implementation, matching the schema this project ships with.
`ExampleCustomSchemaRepository`
(`common/repositories/example_custom_schema.py`) is a second, worked
implementation against a deliberately different, realistic pre-existing
schema (different table name, integer PK instead of `cognito_sub` as
key, generic `event_log`/`failed_jobs` tables reused instead of
dedicated ones) — proving the interface is genuinely schema-agnostic,
not just a rename of the same three tables.

**Where cross-cutting behavior lives**: some logic — like audit-log
failures never masking a successful upsert (decision on failure
isolation, above) — is a property of *how syncing should work*, not of
any specific SQL. That lives once in `SyncService`, wrapping whatever
repository is configured, rather than being re-implemented (and
potentially gotten wrong) inside every custom repository someone writes.
A custom `UserRepository` only needs to get its own storage calls right;
the orchestration around them is already correct.

**Configuration**: which repository is used is decided by
`common/service_factory.py`, via the `REPOSITORY_CLASS` environment
variable (`"module.path:ClassName"`), or the Terraform module's
`repository_class` variable. Defaults to `PostgresUserRepository` if
unset, so the LocalStack demo and a fresh clone work with zero
configuration.

**Trade-off**: this adds a layer of indirection (interface → factory →
concrete implementation) that a single-schema project wouldn't need.
That's an explicit cost accepted for reusability — see
[`docs/extending-the-repository.md`](./extending-the-repository.md) for
the adoption guide this trade-off is meant to pay for.

## What's out of scope for this demo (and why)

- **Multi-tenancy** — a real product would need per-tenant isolation,
  which changes the schema and the `--fix` blast radius significantly.
- **Field-level conflict resolution** — the mismatch-repair logic treats
  Cognito as authoritative for *all* compared fields. A more mature
  version might need per-field ownership rules (e.g. app DB owns a
  `display_name` override that shouldn't be clobbered by Cognito's
  `name` attribute).
