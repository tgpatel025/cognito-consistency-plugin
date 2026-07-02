# Running the demo locally (no AWS account needed)

This uses [LocalStack](https://localstack.cloud) to simulate Cognito and a
real Postgres container for the app database, running the **Postgres
example** repository (`examples/postgres/`) — the core library itself
has no default database, so this demo explicitly configures which one
to use, the same way any real deployment would.

## Prerequisites

- Docker + Docker Compose
- Python 3.12
- AWS CLI (`aws --version`) — LocalStack works with the standard CLI
  pointed at a local endpoint, no `awslocal` wrapper required

## 1. Start the local stack

```bash
cd infra/localstack
docker compose up -d
```

This starts:
- LocalStack on `localhost:4566` (simulating Cognito, Lambda, EventBridge)
- Postgres on `localhost:5432`, auto-initialized with the Postgres
  example's `schema.sql` (`../../examples/postgres/schema.sql`)

## 2. Install Python dependencies

```bash
cd ../..   # back to repo root
pip install -r requirements.txt                    # core library
pip install -r examples/postgres/requirements.txt   # the example this demo uses
```

## 3. Create a demo Cognito user pool + test user

```bash
./scripts/setup_localstack_demo.sh
export USER_POOL_ID=$(cat .demo-user-pool-id)
```

This creates a user pool and one confirmed user (`alice`) directly in
Cognito — but note it does **not** go through the Lambda trigger (that
requires deploying the Lambda into LocalStack, which is a heavier setup
than a quick demo needs). This intentionally simulates the drift
scenario: a user exists in Cognito but the app database doesn't know
about them yet.

## 4. Configure the repository and database connection

```bash
export REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=identity_platform
export DB_USER=postgres
export DB_PASSWORD=postgres
export AWS_ENDPOINT_URL=http://localhost:4566
```

`REPOSITORY_CLASS` is required — without it, `common/service_factory.py`
raises immediately (see `docs/extending-the-repository.md`). The
`DB_HOST`/etc. vars are read by the Postgres example's own connection
helper (`examples/postgres/connection.py`), not by the core library.

## 5. Run the reconciler in report-only mode

Run from the repo root (not `src/`) with `PYTHONPATH=src`, since
`examples.postgres.repository` needs the repo root importable and
`reconciler.run` needs `src` importable — both at once:

```bash
PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL
```

Expected output: one `MISSING_IN_DB` record for `alice`, since she exists
in Cognito but not yet in Postgres.

## 6. Apply the fix

```bash
PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL --fix
```

Re-run step 5 and the drift report should now be empty.

## 7. Inspect the audit trail

```bash
docker exec -it ccp-postgres psql -U postgres -d identity_platform \
  -c "SELECT * FROM sync_audit_log ORDER BY occurred_at;"
```

You should see a `reconciler` event logged for alice's insert.

## 8. Simulate a drift scenario: attribute mismatch

```bash
# Change alice's email directly in Cognito, simulating an out-of-band admin edit
aws --endpoint-url=$AWS_ENDPOINT_URL --region us-east-1 cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username alice \
  --user-attributes Name=email,Value=alice-updated@example.com

PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL
```

Expected: an `ATTRIBUTE_MISMATCH` record showing the `email` field out of
sync.

## 9. Try the dead-letter replay path

The dead-letter path is exercised more directly by the unit tests
(`tests/test_replay_retry_logic.py`) and by manually inserting a row into
`sync_dead_letters`, since triggering an actual Lambda failure requires
deploying the Lambda into LocalStack:

```bash
docker exec -it ccp-postgres psql -U postgres -d identity_platform -c "
INSERT INTO sync_dead_letters (cognito_sub, payload, error, occurred_at, replayed)
VALUES ('demo-sub-123', '{\"username\": \"bob\", \"attributes\": {\"email\": \"bob@example.com\"}}', 'simulated DB timeout', now(), false);
"

PYTHONPATH=src python -m reconciler.replay --dry-run   # preview
PYTHONPATH=src python -m reconciler.replay              # actually replay
```

### Simulating a poison-pill (permanently failing) dead letter

Replay retries are capped at `MAX_RETRY_ATTEMPTS` (5, in
`reconciler/replay.py`) — a dead letter caused by bad data rather than
a transient outage would otherwise fail identically forever. To see this:

```bash
# Insert a dead letter with a NULL cognito_sub payload that will always
# fail the upsert (app_users.cognito_sub is NOT NULL)
docker exec -it ccp-postgres psql -U postgres -d identity_platform -c "
INSERT INTO sync_dead_letters (cognito_sub, payload, error, occurred_at, replayed, retry_count)
VALUES ('bad-sub', '{\"username\": null, \"attributes\": {\"email\": null}}', 'simulated permanent failure', now(), false, 5);
"

PYTHONPATH=src python -m reconciler.replay --report
```

Expected output: this entry shows up under "stuck" rather than being
silently retried again. After fixing the underlying data, reset
`retry_count` to `0` for that row to make it eligible for replay again.

## Tear down

```bash
cd infra/localstack
docker compose down -v
```

## Note on full Lambda deployment in LocalStack

Deploying the actual `post_confirmation`/`post_authentication` Lambdas
into LocalStack and wiring them as real Cognito triggers is possible but
adds meaningful setup complexity (packaging, LocalStack's Lambda executor
config, IAM emulation quirks). This demo intentionally skips that and
instead demonstrates the reconciler against manually-created drift, since
the reconciler is the core "consistency platform" contribution. To
attach the real Lambdas to a real Cognito pool, see the Terraform module
at [`infra/terraform/module`](../infra/terraform/module) and its
[README](../infra/terraform/module/README.md) for how to integrate it
against an existing pool and database.
