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

`sync_audit_log` is append-only and records every sync attempt — this is
the "auditability" piece that regulated environments (HealthTech,
FinTech, GovTech) actually care about, more so than the sync mechanism
itself. See `common/db.py::log_sync_event`.

### 5. Idempotency via upsert on `cognito_sub`

Cognito's own retry behavior, plus the reconciler's replay path, mean the
same event can be processed more than once. All writes key on the
immutable `cognito_sub`, so re-processing an event is a no-op change, not
a duplicate row.

**Why the audit write is separate from, and cannot affect, the primary
write**: `upsert_user()` writes to `app_users` and then calls
`log_sync_event()` as a *second*, independent transaction (see
`common/db.py`). This is intentional, not an oversight: `sync_audit_log`
is a record *about* `app_users`, and a less-critical record should never
be able to veto a more-critical write. Wrapping both in one shared
transaction would mean a full audit-table disk, a misbehaving trigger,
or any other audit-side problem could roll back a successful user sync
— exactly backwards from what you want.

The corollary, which the code enforces explicitly: if the audit write
fails after the `app_users` write has already committed, `upsert_user()`
still returns success and does not raise. The failure is logged loudly
(so the compliance gap is visible in CloudWatch), but it's not allowed to
propagate back to the caller and be mistaken for a sync failure — that
would risk a Lambda handler enqueueing an unnecessary dead letter for a
write that actually succeeded. See `tests/test_upsert_failure_isolation.py`
for the explicit case (audit write fails, primary write is still
reported as successful) and its counterpart (primary write fails, audit
log is never even attempted for a nonexistent row).

**Remaining gap**: because the two writes aren't atomic, there's a
narrow window — the Lambda being frozen or killed between the `app_users`
commit and the `log_sync_event` call — where the audit trail permanently
under-reports relative to `app_users`, with no automatic way to detect or
backfill it. This is judged an acceptable residual risk for a demo (a
single function call is a small window), but a stricter guarantee would
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
[`infra/terraform/alerting.tf`](../infra/terraform/alerting.tf):

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

## What's out of scope for this demo (and why)

- **VPC networking for RDS** — the Terraform config uses a publicly
  accessible RDS instance for simplicity. Production would put Postgres
  in a private subnet and give Lambda VPC access, at the cost of cold
  start latency and NAT Gateway cost.
- **Multi-tenancy** — a real product would need per-tenant isolation,
  which changes the schema and the `--fix` blast radius significantly.
- **Field-level conflict resolution** — the mismatch-repair logic treats
  Cognito as authoritative for *all* compared fields. A more mature
  version might need per-field ownership rules (e.g. app DB owns a
  `display_name` override that shouldn't be clobbered by Cognito's
  `name` attribute).
