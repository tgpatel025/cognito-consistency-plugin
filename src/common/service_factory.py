"""
Builds the SyncService used by the Lambda handlers and reconciler.

Default: PostgresUserRepository, matching infra/localstack/schema.sql --
zero configuration needed beyond the existing DB_SECRET_ARN / DB_HOST
etc. env vars already used for connection details (see common/db.py).

To use your own schema or database engine: implement UserRepository
(see common/repositories/base.py and
common/repositories/example_custom_schema.py for a worked example), then
set the REPOSITORY_CLASS env var to "module.path:ClassName" pointing at
your implementation. No changes to the Lambda handlers, reconciler, or
replay logic are needed -- they all depend on SyncService, which depends
on the UserRepository interface, never on PostgresUserRepository
directly.

Example:
    REPOSITORY_CLASS="my_company.identity_repo:MySQLUserRepository"

The referenced class must be importable from the Lambda's package root
(i.e. bundled into the deployment zip alongside src/, the same way
psycopg2/boto3 are vendored today -- see scripts/build_lambda_deps.sh)
and must accept the same __init__ signature as PostgresUserRepository
(a single connect_fn argument) OR no arguments at all, if your
implementation manages its own connection setup. See
docs/extending-the-repository.md for the full guide.
"""

import os
import importlib

from common.db import get_connection
from common.repositories.postgres import PostgresUserRepository
from common.sync_service import SyncService


def _load_custom_repository_class(dotted_path: str):
    """dotted_path is 'module.path:ClassName'."""
    module_path, _, class_name = dotted_path.partition(":")
    if not module_path or not class_name:
        raise ValueError(
            f"REPOSITORY_CLASS must be in 'module.path:ClassName' form, got: {dotted_path!r}"
        )
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def build_sync_service() -> SyncService:
    custom_class_path = os.environ.get("REPOSITORY_CLASS")

    if custom_class_path:
        repository_class = _load_custom_repository_class(custom_class_path)
        try:
            repository = repository_class(get_connection)
        except TypeError:
            # Implementation manages its own connection setup and takes no args.
            repository = repository_class()
    else:
        repository = PostgresUserRepository(get_connection)

    return SyncService(repository)
