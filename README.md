# Cognito Consistency Platform

A reference implementation of identity synchronization, drift detection,
and reconciliation for applications that use **Amazon Cognito** for
authentication alongside a separate database for business data — a
common architecture, and a common source of subtle data drift. The
library has **no default database or schema** — you bring your own by
implementing a small interface (see
[`docs/extending-the-repository.md`](docs/extending-the-repository.md)),
or use the shipped Postgres example to get running immediately.

> **Note on scope**: this started as an evaluation of a potential AWS
> Marketplace product idea. After research, I concluded the underlying
> pattern is real and worth solving well, but the commercial case is
> weak — see [`docs/market-context.md`](docs/market-context.md) for the
> honest breakdown of why. What's here is a small, correctly-scoped
> systems project demonstrating the pattern, not a startup pitch.

## What it does

```
Cognito User Pool  ──sign-up/sign-in──▶  Lambda triggers  ──sync──▶  Your database
        │                                       │            (via a UserRepository
        │                              on failure: dead-letter    you implement)
        │                                        + audit log
        └──────────────── scheduled reconciler ─────────────────┘
                         (drift detection + report)
```

- **Sync on sign-up and sign-in** via Cognito Post Confirmation / Post
  Authentication Lambda triggers — never blocks the auth flow on a DB
  failure.
- **Drift detection**: a reconciliation engine diffs the full Cognito
  user pool against your database and classifies discrepancies as
  `MISSING_IN_DB`, `ORPHANED_IN_DB`, or `ATTRIBUTE_MISMATCH`.
- **Replay & recovery**: failed sync events are captured as dead letters
  and can be replayed on demand, with a retry limit so a permanently bad
  payload doesn't retry forever.
- **Audit trail**: every sync attempt (success or failure) is logged.
- **Detection and remediation are separate steps** — the reconciler
  reports drift by default; fixes are only applied with an explicit
  `--fix` flag.
- **Bring your own database**: implement one interface
  (`UserRepository`) against your existing schema and engine — Postgres,
  MySQL, DynamoDB, MongoDB, anything. See
  [`docs/extending-the-repository.md`](docs/extending-the-repository.md).

See [`docs/architecture.md`](docs/architecture.md) for the full design
rationale and trade-offs.

## Project structure

```
src/                       # the core library -- zero database dependencies
  lambdas/
    post_confirmation/   # Cognito trigger: fires once on sign-up confirmation
    post_authentication/ # Cognito trigger: fires on every sign-in
  reconciler/
    drift.py             # pure drift-detection logic (no I/O, fully unit tested)
    run.py                # CLI: report or fix drift
    replay.py             # replay failed sync events, with retry limits (--report shows stuck entries)
    scheduled_handler.py  # Lambda entry point for scheduled (EventBridge) runs, publishes CloudWatch metrics
  common/
    sync_service.py        # orchestration layer: audit-failure isolation, wraps any UserRepository
    service_factory.py     # loads your UserRepository via REPOSITORY_CLASS (required -- no default)
    repositories/
      base.py               # UserRepository -- the ONLY thing that ships in core

examples/                  # NOT part of the core library -- starting points to copy/adapt
  postgres/                 # complete, runnable implementation
    repository.py             # PostgresUserRepository
    connection.py              # Secrets Manager or plaintext env vars
    schema.sql                 # app_users / sync_audit_log / sync_dead_letters
    requirements.txt           # psycopg2-binary -- lives ONLY here, not in core
    prepare_for_lambda.sh      # vendors deps + copies into src/ for Terraform packaging
    tests/
  custom_schema_partial/    # partial (3 of 8 methods) -- demonstrates 2 mapping patterns
                            # against a deliberately different, non-Postgres-shaped schema
    repository.py
    tests/

infra/
  terraform/module/       # reusable Terraform module -- attach to an EXISTING Cognito pool + database (see its README)
  localstack/              # local demo environment (runs the Postgres example, no AWS account needed)
docs/
  architecture.md         # design decisions and trade-offs
  extending-the-repository.md  # guide to implementing your own UserRepository
  market-context.md       # honest write-up of the commercial validation behind this
  local-demo.md            # step-by-step guide to running the demo locally
tests/                    # core library tests only -- example-specific tests live in examples/*/tests/
  test_drift.py                     # unit tests for the reconciliation engine
  test_lambda_handlers.py           # Lambda handlers never raise, even under total DB outage
  test_sync_service.py               # audit-log failures never mask a successful primary write
  test_repository_interface.py       # the UserRepository ABC contract itself
  test_service_factory.py            # fail-fast when unconfigured; custom class loading via REPOSITORY_CLASS
  test_replay_retry_logic.py        # dead-letter retry-limit / poison-pill logic
  test_scheduled_handler.py         # CloudWatch metric publishing
  conftest.py                        # sets a test-only REPOSITORY_CLASS so handler modules import cleanly
```

## Quick start (local, no AWS account)

```bash
pip install -r requirements.txt                    # core library (no database drivers)
pip install -r examples/postgres/requirements.txt   # the example used by this quick start
export REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"

pytest tests/                          # core library tests (no database needed)
cd infra/localstack && docker compose up -d
```

Then follow [`docs/local-demo.md`](docs/local-demo.md) for the full
walkthrough (create a Cognito user in LocalStack, run the reconciler,
watch it detect and fix drift). Using your own database instead of the
Postgres example? See
[`docs/extending-the-repository.md`](docs/extending-the-repository.md).

## Deploying into your own AWS environment

This ships as a Terraform **module**
([`infra/terraform/module`](infra/terraform/module)), not a
turnkey "create everything" stack — it's meant to be added to an
**existing** Cognito User Pool and **existing** database, since that's
the situation any real adopter is actually in. It does not create a
User Pool, a database, or a VPC, and it has no default database driver
or repository — you must set `repository_class` (required) to your own
`UserRepository` implementation, or the shipped Postgres example.

If you're using the shipped Postgres example, prepare it for packaging
first (Terraform's `archive_file` only zips `src/`, so the example needs
an explicit copy-in step):

```bash
examples/postgres/prepare_for_lambda.sh   # vendors psycopg2/boto3 and copies the example into src/
```

Then call the module from your own Terraform, pointing it at your
existing pool and (if your repository uses one) a Secrets Manager secret
for your existing database. See
[`infra/terraform/module/README.md`](infra/terraform/module/README.md)
for the full usage example, least-privilege IAM breakdown (each Lambda
gets its own role, scoped to exactly what it does), and a note on why
`lambda_config` is wired in your root module rather than inside this
one.

Set `alert_email` when calling the module to receive notifications for
critical sync failures and accumulating drift — see the "Silent failures
are alarmable" section in [`docs/architecture.md`](docs/architecture.md)
for the two-alarm design.

**Why there's no standalone "create everything from scratch" example**:
an earlier version of this repo included one, but provisioning a brand
new Cognito pool and this module's Lambdas in the same Terraform run
creates a genuine circular dependency (the reconciler's IAM policy needs
the pool's ARN; the pool's `lambda_config` needs the module's Lambda
ARNs) that doesn't exist in the real integration case, since an existing
pool's ARN is already a known value. Rather than paper over that with a
weaker IAM scope just to make a demo path work, the demo path is
[`infra/localstack`](infra/localstack) instead, which exercises the same
sync/reconciliation code without needing any real AWS resources at all.

## Testing

```bash
pytest tests/ -v                                                    # core library (no database)
pytest examples/postgres/tests/ examples/custom_schema_partial/tests/ -v  # examples (needs examples/postgres/requirements.txt)
```

The reconciliation engine (`reconciler/drift.py`) is deliberately written
as a pure function with no AWS or database dependency, so its core logic
is fully unit tested without any infrastructure. Core tests never import
or require a database driver — `conftest.py` supplies a minimal fake
`UserRepository` purely so the Lambda handler modules (which build their
`SyncService` at import time) can be imported during test collection.
CI (`.github/workflows/ci.yml`) runs core tests, example tests, and
`terraform validate` on every push.

## Why this design

A few decisions worth calling out (fully explained in
[`docs/architecture.md`](docs/architecture.md)):

- Lambda triggers **never** raise exceptions back to Cognito — a DB
  failure becomes drift to reconcile later, not a broken sign-up flow.
- Drift **detection** and **remediation** are separate code paths, so a
  bug in the diff logic can't silently corrupt data on an automated
  schedule.
- Orphaned database rows are **flagged, never auto-deleted** — deleting
  business data automatically is judged too risky to automate.
- All writes are **idempotent** (upsert on the immutable Cognito `sub`),
  so retries and replays are always safe.
- The database layer is an **interface** (`UserRepository`), not a fixed
  schema — bring your own `users` table, your own columns, even your own
  database engine. See
  [`docs/extending-the-repository.md`](docs/extending-the-repository.md).
- There is **no default repository or database driver** in core — you
  must configure `REPOSITORY_CLASS`, or the library fails loudly at
  startup rather than silently assuming Postgres.

## License

MIT
