# Extending the repository: using your own database schema or engine

This project ships with a working Postgres implementation
([`infra/localstack/schema.sql`](../infra/localstack/schema.sql):
`app_users`, `sync_audit_log`, `sync_dead_letters`) so it runs out of
the box with zero configuration — but it does not require Postgres, or
that schema, or any particular database engine at all. Every Lambda
handler, the reconciler, and replay depend on an interface —
[`UserRepository`](../src/common/repositories/base.py) — which is just
Python methods over plain dicts, with no SQL or engine assumptions
baked in. If you already have a `users` table (or a DynamoDB table, or
a MongoDB collection) with a completely different shape, you write your
own implementation of that interface and nothing else in this codebase
changes.

## Why this exists

The Terraform side of this project went through the same realization
(see [`docs/architecture.md`](./architecture.md) decision #8): a real
adopter already has their own Cognito pool and their own database. A
project that insists on its own exact schema is no more adoptable than
one that insists on creating its own Cognito pool — it's a migration,
not an integration. The repository interface applies the same principle
one layer down, to the data model instead of the infrastructure.

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
documented in the interface's docstring.

## Worked example

[`src/common/repositories/example_custom_schema.py`](../src/common/repositories/example_custom_schema.py)
demonstrates two mapping patterns against a deliberately different,
realistic pre-existing schema: an integer-PK `users` table with a
nullable `cognito_id` column (not `cognito_sub` as the primary key,
requiring column renaming in `get_all_users`), and a generic
`failed_jobs` table reused for dead letters instead of a dedicated
table. It implements only `upsert_user`, `get_all_users`, and
`enqueue_dead_letter` — enough to show both patterns clearly — rather
than a second complete implementation. The remaining methods would
follow the same two patterns against the same `failed_jobs` table; see
the file's docstring for why it stops there instead of duplicating all
of `postgres.py`'s structure under different names. It's meant to be
copied and adapted, not run as-is — your real schema will differ from
the one imagined there.

## Not just SQL: DynamoDB, MongoDB, or anything else

The interface has no SQL in it — `UserRepository` is just Python methods
taking and returning plain dicts. Nothing about it assumes a relational
database. If your existing user store is DynamoDB, MongoDB, or something
else entirely, the same four steps in "Wiring it in" apply; only the
method bodies change.

Sketch of what a DynamoDB-backed `upsert_user`/`get_all_users` would
look like — not a runnable file, just enough to show the shape carries
over directly:

```python
class DynamoUserRepository(UserRepository):
    def __init__(self, table_name):
        self.table = boto3.resource("dynamodb").Table(table_name)

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

This module doesn't ship a complete DynamoDB or MongoDB implementation,
deliberately: any repository we wrote would encode opinions (single-table
vs. multi-table Dynamo design, which fields get a GSI, Mongo document
shape) that are exactly the kind of decision this interface exists to
leave to you, not re-impose. See
[`docs/architecture.md`](./architecture.md) decision #9 for why the
project stops at the interface rather than shipping a repository per
engine.

## Wiring it in

1. Write your implementation, subclassing `UserRepository` and
   implementing every abstract method (Python's `ABC` machinery will
   raise `TypeError` at instantiation if you miss one — see
   `tests/test_repository_interface.py` for how this is verified).
2. Set the `REPOSITORY_CLASS` environment variable to
   `"your_module.path:YourClassName"` (or, via Terraform, set the
   module's `repository_class` variable — see
   [`infra/terraform/module/README.md`](../infra/terraform/module/README.md)).
3. Bundle your module into the Lambda deployment package alongside
   `src/`, the same way `psycopg2`/`boto3` are vendored today (see
   [`scripts/build_lambda_deps.sh`](../scripts/build_lambda_deps.sh)).
4. Nothing else changes. The Lambda handlers, reconciler, and replay
   logic all depend on `SyncService` (`src/common/sync_service.py`),
   which depends on `UserRepository`, never on `PostgresUserRepository`
   directly. See `src/common/service_factory.py` for exactly how the
   class gets loaded.

## Constructor signature

`build_sync_service()` tries to construct your class with a single
argument (`connect_fn`, matching `PostgresUserRepository`'s signature —
useful if you're also using Postgres/MySQL via a `connect()`-style
function). If that raises `TypeError`, it falls back to a no-argument
constructor, for implementations that manage their own connection setup
(e.g. a DynamoDB repository using boto3's default credential chain, or
one that reads its own connection details from different env vars).

## What you get for free by implementing the interface

Every cross-cutting behavior already built into this project — audit-log
failures never masking a successful sync (`SyncService`), the two-alarm
CloudWatch alerting, the dead-letter retry-limit / poison-pill handling —
works against your schema automatically, because it's implemented once,
against the interface, not duplicated per-repository. You only need to
get your SQL (or API calls, if not using SQL at all) right; the
orchestration logic around it is already correct.
