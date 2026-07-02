"""
Builds the SyncService used by the Lambda handlers and reconciler.

There is no default repository. The core library has no database driver
dependencies and no opinion on which database or schema you use -- see
docs/extending-the-repository.md. You must implement UserRepository
(common/repositories/base.py) and set the REPOSITORY_CLASS environment
variable to "module.path:ClassName" pointing at it. If REPOSITORY_CLASS
is not set, this raises immediately and clearly at startup rather than
silently falling back to some default database you may not have
provisioned or even want.

Example:
    REPOSITORY_CLASS="my_company.identity_repo:MySQLUserRepository"

The referenced class must be importable from the Lambda's package root
(i.e. bundled into the deployment zip alongside src/) and must be
constructible with no arguments -- see "Connection ownership" below.

Connection ownership
---------------------
Earlier versions of this factory passed a shared connect_fn into every
repository's constructor, which meant the core library had to own a
"how to connect" convention -- and in practice, that meant owning a
database driver (psycopg2), even for repositories that don't use
Postgres or SQL at all. That's exactly the kind of opinion this project
is trying not to impose (see docs/architecture.md decision #9).

Now the factory does nothing but import your class and instantiate it
with zero arguments. Your repository is responsible for its own
connection setup, however that looks for your database -- reading env
vars, calling Secrets Manager, opening a connection pool, obtaining a
DynamoDB resource, etc. See examples/postgres/repository.py and
examples/postgres/connection.py for one way to structure this (a
repository that accepts a connect_fn callable in its own constructor,
imported and wired up from within your own REPOSITORY_CLASS-referenced
module, not by this factory) -- but that pattern is a choice your
repository makes, not a contract this factory enforces.
"""

import os
import importlib

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

    if not custom_class_path:
        raise RuntimeError(
            "REPOSITORY_CLASS is not set. This library has no default database "
            "or repository -- you must implement UserRepository "
            "(src/common/repositories/base.py) and set REPOSITORY_CLASS to "
            "'module.path:ClassName' pointing at your implementation. "
            "See docs/extending-the-repository.md, or "
            "examples/postgres/repository.py for a ready-to-use example "
            "(set REPOSITORY_CLASS='examples.postgres.repository:PostgresUserRepository' "
            "and install examples/postgres/requirements.txt)."
        )

    repository_class = _load_custom_repository_class(custom_class_path)
    repository = repository_class()

    return SyncService(repository)
