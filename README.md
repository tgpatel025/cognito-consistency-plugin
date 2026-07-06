# Cognito Consistency Plugin

Identity sync, drift detection, and reconciliation for apps that use
**Amazon Cognito** for auth alongside a separate database for business
data. No default database or schema — implement one small interface
([`docs/extending-the-repository.md`](docs/extending-the-repository.md))
or use the shipped Postgres example.

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
  Authentication Lambda triggers — never blocks auth on a DB failure.
- **Drift detection**: reconciler diffs the full Cognito user pool
  against your database; classifies discrepancies as `MISSING_IN_DB`,
  `ORPHANED_IN_DB`, or `ATTRIBUTE_MISMATCH`.
- **Replay & recovery**: failed sync events are captured as dead letters
  and replayable on demand, with a retry limit so a permanently bad
  payload doesn't retry forever.
- **Audit trail**: every sync attempt (success or failure) is logged.
- **Detection ≠ remediation**: reconciler reports by default; fixes
  require an explicit `--fix` flag.
- **Bring your own database**: implement `UserRepository` against your
  existing schema and engine — Postgres, MySQL, DynamoDB, MongoDB,
  anything.

Design rationale and trade-offs:
[`docs/architecture.md`](docs/architecture.md).

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

Full walkthrough (create a Cognito user in LocalStack, run the
reconciler, watch it detect and fix drift):
[`docs/local-demo.md`](docs/local-demo.md). Using your own database?
[`docs/extending-the-repository.md`](docs/extending-the-repository.md).

## Deploying into your own AWS environment

This ships as a Terraform **module**
([`infra/terraform/module`](infra/terraform/module)) that attaches to an
**existing** Cognito User Pool and **existing** database. It does not
create a User Pool, a database, or a VPC, and has no default repository
— `repository_class` is required.

Using the shipped Postgres example? Prepare it for packaging first
(Terraform's `archive_file` only zips `src/`):

```bash
examples/postgres/prepare_for_lambda.sh   # vendors psycopg2/boto3 and copies the example into src/
```

Then call the module from your own Terraform, pointing it at your
existing pool and (if your repository uses one) a Secrets Manager secret
for your database. See
[`infra/terraform/module/README.md`](infra/terraform/module/README.md)
for the full usage example, the per-Lambda least-privilege IAM
breakdown, and why `lambda_config` is wired in your root module.

Set `alert_email` to get notified of critical sync failures and
accumulating drift — see "Silent failures are alarmable" in
[`docs/architecture.md`](docs/architecture.md) for the two-alarm design.

**Why no "create everything from scratch" example**: provisioning a
brand-new pool and this module in one Terraform run creates a genuine
circular dependency (the reconciler's IAM policy needs the pool's ARN;
the pool's `lambda_config` needs the module's Lambda ARNs). That cycle
doesn't exist when attaching to an existing pool. The demo path is
[`infra/localstack`](infra/localstack) instead — same code, no real AWS
resources.

## Testing

```bash
pytest tests/ -v                                                    # core library (no database)
pytest examples/postgres/tests/ examples/custom_schema_partial/tests/ -v  # examples (needs examples/postgres/requirements.txt)
```

`reconciler/drift.py` is a pure function with no AWS or database
dependency, so it's fully unit tested without infrastructure. Core tests
never require a database driver — `conftest.py` supplies a minimal fake
`UserRepository` so Lambda handler modules (which build their
`SyncService` at import time) import cleanly. CI
(`.github/workflows/ci.yml`) runs core tests, example tests, and
`terraform validate` on every push.

## Why this design

Full explanations in [`docs/architecture.md`](docs/architecture.md):

- Lambda triggers **never** raise back to Cognito — a DB failure becomes
  drift to reconcile later, not a broken sign-up flow.
- Drift **detection** and **remediation** are separate code paths, so a
  diff-logic bug can't silently corrupt data on a schedule.
- Orphaned database rows are **flagged, never auto-deleted**.
- All writes are **idempotent** (upsert on the immutable Cognito `sub`),
  so retries and replays are always safe.
- The database layer is an **interface** (`UserRepository`), not a fixed
  schema — see
  [`docs/extending-the-repository.md`](docs/extending-the-repository.md).
- **No default repository or driver** in core — set `REPOSITORY_CLASS`
  or the library fails loudly at startup.

## License

MIT
