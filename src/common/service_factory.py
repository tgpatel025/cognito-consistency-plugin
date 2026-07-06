"""
Builds the SyncService used by the Lambda handlers and reconciler.

No default repository: implement UserRepository (common/repositories/
base.py) and set REPOSITORY_CLASS="module.path:ClassName" pointing at
it. Unset -> raises at startup, loudly, instead of falling back to a
database you may not have.

    REPOSITORY_CLASS="my_company.identity_repo:MySQLUserRepository"

Your class must be bundled in the deployment zip alongside src/ and
constructible with no arguments -- the factory just imports and
instantiates it. Connection setup (env vars, Secrets Manager, pools,
DynamoDB resources...) is entirely the repository's own business; see
examples/postgres/ for one way to structure it, and
docs/extending-the-repository.md for the guide.
"""

import os
import importlib

from common.repositories.base import UserRepository
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

    if not isinstance(repository, UserRepository):
        raise TypeError(
            f"REPOSITORY_CLASS ({custom_class_path!r}) does not implement "
            "UserRepository (src/common/repositories/base.py). Subclass "
            "UserRepository so missing methods are caught at startup, not "
            "as an AttributeError on the first real sync event."
        )

    return SyncService(repository)
