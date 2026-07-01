"""
Reconciliation engine.

Compares the Cognito User Pool (source of truth for identity) against the
application database (source of truth for business data) and classifies
every discrepancy into one of four drift categories:

  MISSING_IN_DB       - user exists in Cognito, no row in app_users
                         (e.g. the post_confirmation trigger failed silently)
  ORPHANED_IN_DB       - row exists in app_users, user no longer in Cognito
                         (e.g. user was deleted directly in Cognito)
  ATTRIBUTE_MISMATCH   - both exist, but email/username/attributes differ
                         (e.g. post_authentication trigger never fired,
                         or an admin edited attributes out-of-band)
  IN_SYNC              - no action needed

This module is intentionally side-effect-free for the "detect" phase --
`find_drift()` only reads. Fixing drift is a separate, explicit step
(see reconcile.py / replay.py) so the tool never silently overwrites data
without an operator being able to see the diff first. That separation is
the main design decision worth defending in an interview: detection and
remediation are different trust levels and should not be coupled.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class DriftType(str, Enum):
    MISSING_IN_DB = "MISSING_IN_DB"
    ORPHANED_IN_DB = "ORPHANED_IN_DB"
    ATTRIBUTE_MISMATCH = "ATTRIBUTE_MISMATCH"


@dataclass
class DriftRecord:
    cognito_sub: str
    drift_type: DriftType
    cognito_data: Optional[dict] = None
    db_data: Optional[dict] = None
    mismatched_fields: list = field(default_factory=list)


COMPARED_FIELDS = ("email", "username")


def _normalize_cognito_user(cognito_user: dict) -> dict:
    """Cognito's list_users response nests attributes as a list of
    {Name, Value} dicts. Flatten to a simple dict keyed by sub."""
    attrs = {a["Name"]: a["Value"] for a in cognito_user.get("Attributes", [])}
    return {
        "cognito_sub": attrs.get("sub"),
        "email": attrs.get("email"),
        "username": cognito_user.get("Username"),
        "attributes": attrs,
    }


def find_drift(cognito_users: list, db_users: list) -> list:
    """
    cognito_users: raw output of cognito-idp list-users (list of dicts)
    db_users: rows from app_users (list of dicts with cognito_sub, email, username, attributes)

    Returns a list of DriftRecord.
    """
    cognito_by_sub = {}
    for raw in cognito_users:
        normalized = _normalize_cognito_user(raw)
        if normalized["cognito_sub"]:
            cognito_by_sub[normalized["cognito_sub"]] = normalized

    db_by_sub = {row["cognito_sub"]: row for row in db_users}

    drift = []

    # Users in Cognito but missing from the DB
    for sub, cognito_user in cognito_by_sub.items():
        if sub not in db_by_sub:
            drift.append(
                DriftRecord(
                    cognito_sub=sub,
                    drift_type=DriftType.MISSING_IN_DB,
                    cognito_data=cognito_user,
                )
            )
            continue

        db_user = db_by_sub[sub]
        mismatches = [
            field_name
            for field_name in COMPARED_FIELDS
            if cognito_user.get(field_name) != db_user.get(field_name)
        ]
        if mismatches:
            drift.append(
                DriftRecord(
                    cognito_sub=sub,
                    drift_type=DriftType.ATTRIBUTE_MISMATCH,
                    cognito_data=cognito_user,
                    db_data=db_user,
                    mismatched_fields=mismatches,
                )
            )

    # Rows in the DB with no corresponding Cognito user
    for sub, db_user in db_by_sub.items():
        if sub not in cognito_by_sub:
            drift.append(
                DriftRecord(
                    cognito_sub=sub,
                    drift_type=DriftType.ORPHANED_IN_DB,
                    db_data=db_user,
                )
            )

    logger.info("Reconciliation found %d drift record(s)", len(drift))
    return drift


def summarize(drift_records: list) -> dict:
    """Small helper for CLI/report output."""
    summary = {t.value: 0 for t in DriftType}
    for record in drift_records:
        summary[record.drift_type.value] += 1
    summary["total"] = len(drift_records)
    return summary
