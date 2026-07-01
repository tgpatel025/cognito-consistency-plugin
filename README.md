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
    replay.py             # replay failed sync events from the dead-letter table
    scheduled_handler.py  # Lambda entry point for scheduled (EventBridge) runs
  common/
    db.py                 # Postgres access layer (upsert, audit log, dead letters)
infra/
  terraform/              # real AWS deployment (Cognito, RDS, Lambda, EventBridge)
  localstack/              # local demo environment (LocalStack + Postgres, no AWS account needed)
docs/
  architecture.md         # design decisions and trade-offs
  market-context.md       # honest write-up of the commercial validation behind this
  local-demo.md            # step-by-step guide to running the demo locally
tests/
  test_drift.py            # unit tests for the reconciliation engine
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

## Real AWS deployment

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in a real db_password
../../scripts/build_lambda_deps.sh              # vendor psycopg2/boto3 into src/
terraform init
terraform apply
```

This provisions a Cognito User Pool, an RDS Postgres instance, the two
sync Lambdas wired as Cognito triggers, and a reconciler Lambda on a
15-minute EventBridge schedule. See
[`infra/terraform/variables.tf`](infra/terraform/variables.tf) for all
configurable inputs.

Set `alert_email` in your `terraform.tfvars` to receive notifications for
critical sync failures and accumulating drift (see
[`infra/terraform/alerting.tf`](infra/terraform/alerting.tf) and the
"Silent failures are alarmable" section in
[`docs/architecture.md`](docs/architecture.md)). AWS will send a
subscription-confirmation email you need to accept before alerts start
flowing.

**Cost note**: this uses a `db.t3.micro` RDS instance and Lambda, both
inexpensive but not free. Run `terraform destroy` when done experimenting.

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
