# Extending the repository: using your own database schema or engine

This project has **no default database, schema, or repository**. Every
Lambda handler, the reconciler, and replay depend on one interface —
[`UserRepository`](../src/common/repositories/base.py) — plain Python
methods over plain dicts, with no SQL or engine assumptions. Implement it
against your own `users` table (or DynamoDB table, MongoDB collection,
anything else), point `REPOSITORY_CLASS` at your implementation, and
nothing else changes.

Want something runnable immediately? [`examples/postgres`](../examples/postgres)
is a complete, working implementation you can use directly or copy — see
"Using the shipped example" below.

## Why there's no default

A default repository means the core library ships a driver (`psycopg2`, a
compiled binary dependency) and an opinion about database connections that
isn't engine-independent. This library owns the sync/reconciliation
*logic* — not a database, driver, or schema (see
[`docs/architecture.md`](./architecture.md) decisions #8 and #9).

The practical result: `REPOSITORY_CLASS` is required. If unset,
`common/service_factory.py::build_sync_service()` raises immediately at
Lambda cold-start — not silently on first invocation — with a message
pointing at this doc and the shipped example.

## The interface

See [`src/common/repositories/base.py`](../src/common/repositories/base.py)
for the full, documented contract. You implement:

| Method | Purpose |
|---|---|
| `upsert_user(cognito_sub, email, username, attributes)` | Create or update a user record. Must be idempotent. |
| `get_all_users()` | Return every synced user, for the reconciler to diff against Cognito. |
| `log_sync_event(cognito_sub, event_source, status, detail)` | Append an audit record. |
| `enqueue_dead_letter(cognito_sub, payload, error)` | Record a failed sync for later replay. `payload` is opaque to you -- store and return it unchanged; it's written/read as `{"username": str \| None, "attributes": dict}`. |
| `fetch_unreplayed_dead_letters(max_retry)` | Dead letters eligible for retry. |
| `fetch_stuck_dead_letters(max_retry)` | Dead letters that exceeded the retry limit. |
| `mark_dead_letter_replayed(id)` | Mark a dead letter successfully replayed. |
| `record_dead_letter_failure(id, error)` | Increment retry count after a failed replay. |

Return shapes (dict keys the rest of the codebase expects) are documented
in the interface's docstring. Implement every method — Python's `ABC`
machinery raises `TypeError` at instantiation if you miss one (see
`tests/test_repository_interface.py`), so gaps are caught immediately.

## Connection ownership: entirely yours

`build_sync_service()` constructs your class with **zero arguments**:
`repository_class()`. How your repository connects (env vars, Secrets
Manager, a connection pool, boto3's default credential chain) is entirely
up to you, decided inside your own constructor or module.

If your constructor needs setup, do it inside `__init__` (read env vars,
call Secrets Manager, etc.) rather than accepting constructor arguments —
the factory has no way to supply them.

## Examples in this repo

Two examples live in [`examples/`](../examples), kept outside `src/` so
it's never ambiguous what's library versus starting point:

### `examples/postgres/` — complete, runnable

A full `UserRepository` implementation against a real Postgres schema
([`examples/postgres/schema.sql`](../examples/postgres/schema.sql)):
`app_users`, `sync_audit_log`, `sync_dead_letters`. This is what the
LocalStack demo runs against. It has its own
[`requirements.txt`](../examples/postgres/requirements.txt)
(`psycopg2-binary`) — **not** part of the core project's dependencies —
and its own connection helper
([`connection.py`](../examples/postgres/connection.py), Secrets Manager
or plaintext env vars).

To use it as-is:
```bash
pip install -r examples/postgres/requirements.txt
export REPOSITORY_CLASS="examples.postgres.repository:PostgresUserRepository"
```
For a real Lambda deployment, see
[`examples/postgres/prepare_for_lambda.sh`](../examples/postgres/prepare_for_lambda.sh) —
Terraform's packaging only zips `src/`, so `examples/` needs an explicit
copy-in step (see that script and
[`infra/terraform/module/README.md`](../infra/terraform/module/README.md)).

### `examples/custom_schema_partial/` — partial, for pattern reference

Shows two mapping patterns against a very different schema: an integer-PK
`users` table with a nullable `cognito_id` column (instead of
`cognito_sub` as the key), and a generic pre-existing `failed_jobs` table
reused for dead letters. It implements only 3 of the 8 methods — enough
to show both patterns; see its docstring for why it stays partial. Read
and adapt, don't run as-is.

### Not just SQL: DynamoDB, MongoDB, or anything else

The interface has no SQL in it. Sketch of a DynamoDB-backed
`upsert_user`/`get_all_users` (not a runnable file — just the shape):

```python
class DynamoUserRepository(UserRepository):
    def __init__(self):
        self.table = boto3.resource("dynamodb").Table(os.environ["USERS_TABLE_NAME"])

    def upsert_user(self, cognito_sub, email, username, attributes):
        # cognito_sub as the partition key -- idempotent by construction,
        # a put_item with the same key just overwrites
        self.table.put_item(Item={
            "pk": cognito_sub, "email": email, "username": username,
            "attributes": attributes,
        })
        return {"id": cognito_sub, "inserted": True}  # Dynamo doesn't
        # distinguish insert/update without a conditional check; approximate
        # or add one with ConditionExpression if that distinction matters
        # to your audit trail.

    def get_all_users(self):
        # Table.scan() paginates internally here for simplicity; a large
        # table would want to loop on LastEvaluatedKey.
        response = self.table.scan()
        return [
            {"cognito_sub": item["pk"], "email": item.get("email"),
             "username": item.get("username"), "attributes": item.get("attributes", {})}
            for item in response["Items"]
        ]
    # ... remaining methods follow the same shape against a dead-letters table/GSI.
```

Note the zero-argument constructor reading config from an env var
(`USERS_TABLE_NAME`) — the repository's own choice, not something the
factory imposes (see "Connection ownership" above).

No complete DynamoDB or MongoDB implementation ships here, deliberately:
it would encode opinions (single- vs. multi-table design, GSI choices,
document shape) that this interface exists to leave to you.

## Wiring it in

1. Write your implementation: subclass `UserRepository`, implement every
   abstract method, constructor with zero required arguments.
2. Set the `REPOSITORY_CLASS` environment variable to
   `"your_module.path:YourClassName"` (or, via Terraform, the module's
   required `repository_class` variable — see
   [`infra/terraform/module/README.md`](../infra/terraform/module/README.md)).
3. Bundle your module into the Lambda deployment package alongside
   `src/` — Terraform's `archive_file` only zips `src/`. Use
   [`examples/postgres/prepare_for_lambda.sh`](../examples/postgres/prepare_for_lambda.sh)
   as a template for your own vendoring step.
4. Nothing else changes. Handlers, reconciler, and replay all depend on
   `SyncService` (`src/common/sync_service.py`), which depends on
   `UserRepository`, never on a concrete implementation. See
   `src/common/service_factory.py` for how the class gets loaded.

## What you get for free by implementing the interface

Every cross-cutting behavior — audit-log failures never masking a
successful sync (`SyncService`), the two-alarm CloudWatch alerting, the
dead-letter retry-limit / poison-pill handling — works against your
schema automatically, implemented once against the interface. Get your
storage calls right; the orchestration around them is already correct.
