# Running the demo locally (no AWS account needed)

Uses [LocalStack](https://localstack.cloud) to simulate Cognito, a real
Postgres container for the app database, and the **Postgres example**
repository (`examples/postgres/`) — the core library has no default
database, so the demo configures one explicitly, same as any real deployment.

## Prerequisites

- Docker + Docker Compose
- Python 3.12
- AWS CLI (`aws --version`) — the standard CLI pointed at a local endpoint
  works; no `awslocal` wrapper needed

## 1. Start the local stack

```bash
cd infra/localstack
docker compose up -d
```

This starts:
- LocalStack on `localhost:4566` (Cognito, Lambda, EventBridge)
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

Creates a user pool and one confirmed user (`alice`) directly in Cognito,
**bypassing the Lambda trigger** — deliberately creating drift: alice
exists in Cognito but not in the app database.

## 4. Configure the repository and database connection

```bash
export REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"
export ALLOW_PLAINTEXT_DB_CREDS=1
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=identity_platform
export DB_USER=postgres
export DB_PASSWORD=postgres
export AWS_ENDPOINT_URL=http://localhost:4566
```

`REPOSITORY_CLASS` is required — without it, `common/service_factory.py`
raises immediately (see `docs/extending-the-repository.md`). The `DB_*`
vars are read by the example's own connection helper
(`examples/postgres/connection.py`), not the core library.
`ALLOW_PLAINTEXT_DB_CREDS=1` opts into the plaintext env-var fallback —
without it (and no `DB_SECRET_ARN`), the connection helper fails loudly
instead of silently using default credentials.

## 5. Run the reconciler in report-only mode

Run from the repo root (not `src/`) with `PYTHONPATH=src` —
`examples.postgres.repository` needs the repo root importable and
`reconciler.run` needs `src` importable:

```bash
PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL
```

Expected: one `MISSING_IN_DB` record for `alice`.

## 6. Apply the fix

```bash
PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL --fix
```

Re-run step 5 — the drift report should now be empty.

## 7. Inspect the audit trail

```bash
docker exec -it ccp-postgres psql -U postgres -d identity_platform \
  -c "SELECT * FROM sync_audit_log ORDER BY occurred_at;"
```

Expected: a `reconciler` event for alice's insert.

## 8. Simulate a drift scenario: attribute mismatch

```bash
# Change alice's email directly in Cognito, simulating an out-of-band admin edit
aws --endpoint-url=$AWS_ENDPOINT_URL --region us-east-1 cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username alice \
  --user-attributes Name=email,Value=alice-updated@example.com

PYTHONPATH=src python -m reconciler.run --user-pool-id $USER_POOL_ID --endpoint-url $AWS_ENDPOINT_URL
```

Expected: an `ATTRIBUTE_MISMATCH` record showing `email` out of sync.

## 9. Try the dead-letter replay path

Triggering a real Lambda failure requires deploying the Lambda into
LocalStack, so insert a dead-letter row manually instead (the unit tests
in `tests/test_replay_retry_logic.py` cover this path more directly):

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
`reconciler/replay.py`) — bad data would otherwise fail identically
forever. To see this:

```bash
# Insert a dead letter with a NULL cognito_sub payload that will always
# fail the upsert (app_users.cognito_sub is NOT NULL)
docker exec -it ccp-postgres psql -U postgres -d identity_platform -c "
INSERT INTO sync_dead_letters (cognito_sub, payload, error, occurred_at, replayed, retry_count)
VALUES ('bad-sub', '{\"username\": null, \"attributes\": {\"email\": null}}', 'simulated permanent failure', now(), false, 5);
"

PYTHONPATH=src python -m reconciler.replay --report
```

Expected: the entry shows up under "stuck" instead of being retried again.
After fixing the underlying data, reset `retry_count` to `0` to make it
eligible for replay again.

## Tear down

```bash
cd infra/localstack
docker compose down -v
```

## Note on full Lambda deployment in LocalStack

Deploying the real `post_confirmation`/`post_authentication` Lambdas into
LocalStack as actual Cognito triggers is possible but adds meaningful setup
complexity (packaging, Lambda executor config, IAM emulation quirks). This
demo skips it and demonstrates the reconciler against manually-created
drift. To attach the real Lambdas to a real Cognito pool, see the Terraform
module at [`infra/terraform/module`](../infra/terraform/module) and its
[README](../infra/terraform/module/README.md).
