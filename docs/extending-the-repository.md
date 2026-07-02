# Extending the repository: using your own database schema or engine

This project has **no default database, schema, or repository**. Every
Lambda handler, the reconciler, and replay depend on an interface —
[`UserRepository`](../src/common/repositories/base.py) — which is just
Python methods over plain dicts, with no SQL or engine assumptions baked
in. You implement that interface against your own `users` table (or
DynamoDB table, or MongoDB collection, or anything else), point
`REPOSITORY_CLASS` at your implementation, and nothing else in this
codebase changes.

If you want something runnable immediately rather than starting from a
blank page, [`examples/postgres`](../examples/postgres) is a complete,
working implementation you can point at directly or copy and adapt —
see "Using the shipped example" below.

## Why there's no default

An earlier version of this project shipped a default Postgres
repository, reasoning that "the schema is pluggable, Postgres is just a
convenience." In practice that still meant every deployment carried
`psycopg2` (a compiled binary dependency) whether or not it was used,
and the core library owned an opinion — "here's how you connect to a
database" — that isn't actually engine-independent the moment it imports
a specific driver. That's the same mistake the Terraform module used to
make by creating its own Cognito pool and RDS instance (see
[`docs/architecture.md`](./architecture.md) decision #8) and the same
mistake the repository interface itself was built to avoid at the
schema level (decision #9). Removing the default is applying that
principle consistently: this library owns the sync/reconciliation
*logic*, not a database, not a driver, not a schema.

The practical result: `REPOSITORY_CLASS` is required. If it's not set,
`common/service_factory.py::build_sync_service()` raises immediately and
clearly — at Lambda cold-start, not silently on first invocation — with
a message pointing at this doc and the shipped example.

## The interface

See [`src/common/repositories/base.py`](../src/common/repositories/base.py)
for the full, documented contract. In short, you implement:

| Method | Purpose |
|---|---|
| `upsert_user(cognito_sub, email, username, attributes)` | Create or update a user record. Must be idempotent. |
| `get_all_users()` | Return every synced user, for the reconciler to diff against Cognito. |
| `log_sync_event(cognito_sub, event_source, status, detail)` | Append an audit record. |
| `enqueue_dead_letter(cognito_sub, payload, error)` | Record a failed sync for later replay. |
| `fetch_unreplayed_dead_letters(max_retry)` | Dead letters eligible for retry. |
| `fetch_stuck_dead_letters(max_retry)` | Dead letters that exceeded the retry limit. |
| `mark_dead_letter_replayed(id)` | Mark a dead letter successfully replayed. |
| `record_dead_letter_failure(id, error)` | Increment retry count after a failed replay. |

Return shapes (dict keys expected by the rest of the codebase) are
documented in the interface's docstring. Your class must implement every
method — Python's `ABC` machinery raises `TypeError` at instantiation if
you miss one (see `tests/test_repository_interface.py`), so a missing
method is caught immediately, not discovered later when the reconciler
happens to call it.

## Connection ownership: entirely yours

`build_sync_service()` constructs your class with **zero arguments**:
`repository_class()`. There is no shared `connect_fn` convention the
core library provides or expects — how your repository connects to its
database (env vars, Secrets Manager, a connection pool, a DynamoDB
resource via boto3's default credential chain, whatever) is entirely up
to you, decided inside your own constructor or module.

If your constructor needs setup that can't reasonably have zero-argument
defaults, do that setup inside `__init__` itself (reading env vars,
calling Secrets Manager, etc.) rather than accepting constructor
arguments — the factory has no way to supply them.

## Examples in this repo

Two examples live in [`examples/`](../examples), a separate top-level
directory from `src/` — kept apart deliberately so it's never ambiguous
which files are the actual library versus a starting point to copy:

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
[`examples/postgres/prepare_for_lambda.sh`](../examples/postgres/prepare_for_lambda.sh),
since Terraform's packaging only zips `src/` (see that script and
[`infra/terraform/module/README.md`](../infra/terraform/module/README.md)
for why `examples/` needs an explicit copy-in step, not automatic
bundling).

### `examples/custom_schema_partial/` — partial, for pattern reference

Demonstrates two mapping patterns against a schema that looks nothing
like the Postgres example's — an integer-PK `users` table with a
nullable `cognito_id` column instead of `cognito_sub` as the key, and a
generic pre-existing `failed_jobs` table reused for dead letters instead
of a dedicated table. It implements only 3 of the 8 interface methods
(enough to show both patterns clearly) rather than a second complete
implementation — see its own docstring for why it stays partial. Meant
to be read and adapted, not run as-is.

### Not just SQL: DynamoDB, MongoDB, or anything else

Both shipped examples happen to use Postgres/SQL, but the interface
itself has no SQL in it. Sketch of what a DynamoDB-backed
`upsert_user`/`get_all_users` would look like — not a runnable file,
just enough to show the shape carries over directly to a key-value
store:

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

Note the zero-argument constructor reading configuration from an env var
(`USERS_TABLE_NAME`) rather than accepting a `connect_fn` parameter —
that's a choice this repository makes for itself, not something the
factory imposes (see "Connection ownership" above).

This module doesn't ship a complete DynamoDB or MongoDB implementation,
deliberately: any repository written here would encode opinions
(single-table vs. multi-table Dynamo design, which fields get a GSI,
Mongo document shape) that are exactly the kind of decision this
interface exists to leave to you, not re-impose.

## Wiring it in

1. Write your implementation, subclassing `UserRepository`, implementing
   every abstract method, with a constructor that takes zero required
   arguments.
2. Set the `REPOSITORY_CLASS` environment variable to
   `"your_module.path:YourClassName"` (or, via Terraform, the module's
   `repository_class` variable, which is required — see
   [`infra/terraform/module/README.md`](../infra/terraform/module/README.md)).
3. Bundle your module into the Lambda deployment package alongside
   `src/` (Terraform's `archive_file` only zips `src/` — see
   [`examples/postgres/prepare_for_lambda.sh`](../examples/postgres/prepare_for_lambda.sh)
   for the pattern this project's own example uses, as a template for
   your own vendoring step).
4. Nothing else changes. The Lambda handlers, reconciler, and replay
   logic all depend on `SyncService` (`src/common/sync_service.py`),
   which depends on `UserRepository`, never on any concrete
   implementation. See `src/common/service_factory.py` for exactly how
   the class gets loaded and instantiated.

## What you get for free by implementing the interface

Every cross-cutting behavior already built into this project — audit-log
failures never masking a successful sync (`SyncService`), the two-alarm
CloudWatch alerting, the dead-letter retry-limit / poison-pill handling —
works against your schema automatically, because it's implemented once,
against the interface, not duplicated per-repository. You only need to
get your own storage calls right; the orchestration logic around them is
already correct.
