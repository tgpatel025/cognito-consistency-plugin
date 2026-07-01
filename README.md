# Cognito Consistency Platform

A reference implementation of identity synchronization, drift detection,
and reconciliation for applications that use **Amazon Cognito** for
authentication alongside a separate **Postgres** database for business
data — a common architecture, and a common source of subtle data drift.

> **Note on scope**: this started as an evaluation of a potential AWS
> Marketplace product idea. After research, I concluded the underlying
> pattern is real and worth solving well, but the commercial case is
> weak — see [`docs/market-context.md`](docs/market-context.md) for the
> honest breakdown of why. What's here is a small, correctly-scoped
> systems project demonstrating the pattern, not a startup pitch.

## What it does

```
Cognito User Pool  ──sign-up/sign-in──▶  Lambda triggers  ──sync──▶  Postgres (app_users)
        │                                       │
        │                              on failure: dead-letter + audit log
        │
        └──────────────── scheduled reconciler ─────────────────┘
                         (drift detection + report)
```

- **Sync on sign-up and sign-in** via Cognito Post Confirmation / Post
  Authentication Lambda triggers — never blocks the auth flow on a DB
  failure.
- **Drift detection**: a reconciliation engine diffs the full Cognito
  user pool against the app database and classifies discrepancies as
  `MISSING_IN_DB`, `ORPHANED_IN_DB`, or `ATTRIBUTE_MISMATCH`.
- **Replay & recovery**: failed sync events are captured in a
  dead-letter table and can be replayed on demand.
- **Audit trail**: every sync attempt (success or failure) is logged to
  an append-only table.
- **Detection and remediation are separate steps** — the reconciler
  reports drift by default; fixes are only applied with an explicit
  `--fix` flag.

See [`docs/architecture.md`](docs/architecture.md) for the full design
rationale and trade-offs.

## Project structure

```
src/
  lambdas/
    post_confirmation/   # Cognito trigger: fires once on sign-up confirmation
    post_authentication/ # Cognito trigger: fires on every sign-in
  reconciler/
    drift.py             # pure drift-detection logic (no I/O, fully unit tested)
    run.py                # CLI: report or fix drift
    replay.py             # replay failed sync events, with retry limits (--report shows stuck entries)
    scheduled_handler.py  # Lambda entry point for scheduled (EventBridge) runs, publishes CloudWatch metrics
  common/
    db.py                 # Postgres access layer (upsert, audit log, dead letters, Secrets Manager or plaintext env creds)
infra/
  terraform/module/       # reusable Terraform module -- attach to an EXISTING Cognito pool + database (see its README)
  localstack/              # local demo environment (LocalStack + Postgres, no AWS account needed)
docs/
  architecture.md         # design decisions and trade-offs
  market-context.md       # honest write-up of the commercial validation behind this
  local-demo.md            # step-by-step guide to running the demo locally
tests/
  test_drift.py                     # unit tests for the reconciliation engine
  test_lambda_handlers.py           # Lambda handlers never raise, even under total DB outage
  test_upsert_failure_isolation.py  # audit-log failures never mask a successful primary write
  test_replay_retry_logic.py        # dead-letter retry-limit / poison-pill logic
  test_scheduled_handler.py         # CloudWatch metric publishing
  test_db_credentials.py            # Secrets Manager vs. plaintext env var credential paths
```

## Quick start (local, no AWS account)

```bash
pip install -r requirements.txt
pytest tests/                          # run the unit-tested core logic
cd infra/localstack && docker compose up -d
```

Then follow [`docs/local-demo.md`](docs/local-demo.md) for the full
walkthrough (create a Cognito user in LocalStack, run the reconciler,
watch it detect and fix drift).

## Deploying into your own AWS environment

This ships as a Terraform **module**
([`infra/terraform/module`](infra/terraform/module)), not a
turnkey "create everything" stack — it's meant to be added to an
**existing** Cognito User Pool and **existing** database, since that's
the situation any real adopter is actually in. It does not create a
User Pool, a database, or a VPC.

```bash
../../scripts/build_lambda_deps.sh   # vendor psycopg2/boto3 into src/ before packaging
```

Then call the module from your own Terraform, pointing it at your
existing pool and a Secrets Manager secret for your existing database.
See [`infra/terraform/module/README.md`](infra/terraform/module/README.md)
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
pytest tests/ -v
```

The reconciliation engine (`reconciler/drift.py`) is deliberately written
as a pure function with no AWS or database dependency, so its core logic
is fully unit tested without any infrastructure. CI (`.github/workflows/ci.yml`)
runs these tests plus `terraform validate` on every push.

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

## License

MIT
