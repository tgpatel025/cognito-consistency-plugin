"""
Unit tests for drift detection. These run with no AWS or DB dependency --
`find_drift` is pure logic over two in-memory lists, which is exactly why
it was written as a separate, side-effect-free module.

Run with: pytest tests/test_drift.py
"""

from reconciler.drift import find_drift, summarize, DriftType


def cognito_user(sub, email, username, extra_attrs=None):
    attrs = [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}]
    if extra_attrs:
        attrs.extend({"Name": k, "Value": v} for k, v in extra_attrs.items())
    return {"Username": username, "Attributes": attrs}


def db_user(sub, email, username, attributes=None):
    if attributes is None:
        attributes = {"sub": sub, "email": email}
    return {"cognito_sub": sub, "email": email, "username": username, "attributes": attributes}


def test_in_sync_produces_no_drift():
    cognito_users = [cognito_user("sub-1", "a@example.com", "alice")]
    db_users = [db_user("sub-1", "a@example.com", "alice")]

    drift = find_drift(cognito_users, db_users)

    assert drift == []


def test_missing_in_db_detected():
    cognito_users = [cognito_user("sub-1", "a@example.com", "alice")]
    db_users = []

    drift = find_drift(cognito_users, db_users)

    assert len(drift) == 1
    assert drift[0].drift_type == DriftType.MISSING_IN_DB
    assert drift[0].cognito_sub == "sub-1"


def test_orphaned_in_db_detected():
    cognito_users = []
    db_users = [db_user("sub-1", "a@example.com", "alice")]

    drift = find_drift(cognito_users, db_users)

    assert len(drift) == 1
    assert drift[0].drift_type == DriftType.ORPHANED_IN_DB


def test_attribute_mismatch_detected_on_email_change():
    cognito_users = [cognito_user("sub-1", "new@example.com", "alice")]
    db_users = [db_user("sub-1", "old@example.com", "alice")]

    drift = find_drift(cognito_users, db_users)

    assert len(drift) == 1
    assert drift[0].drift_type == DriftType.ATTRIBUTE_MISMATCH
    # email is both a top-level compared field and part of the raw
    # attributes dict, so a real email change shows up in both.
    assert drift[0].mismatched_fields == ["email", "attributes"]


def test_attribute_mismatch_detected_on_username_change():
    cognito_users = [cognito_user("sub-1", "a@example.com", "alice_new")]
    db_users = [db_user("sub-1", "a@example.com", "alice_old")]

    drift = find_drift(cognito_users, db_users)

    assert len(drift) == 1
    assert drift[0].mismatched_fields == ["username"]


def test_mixed_scenario_all_three_drift_types():
    cognito_users = [
        cognito_user("sub-1", "a@example.com", "alice"),   # in sync
        cognito_user("sub-2", "b@example.com", "bob"),      # missing in db
        cognito_user("sub-3", "new@example.com", "carol"),  # mismatch
    ]
    db_users = [
        db_user("sub-1", "a@example.com", "alice"),
        db_user("sub-3", "old@example.com", "carol"),
        db_user("sub-4", "d@example.com", "dave"),          # orphaned
    ]

    drift = find_drift(cognito_users, db_users)
    summary = summarize(drift)

    assert summary["total"] == 3
    assert summary[DriftType.MISSING_IN_DB.value] == 1
    assert summary[DriftType.ATTRIBUTE_MISMATCH.value] == 1
    assert summary[DriftType.ORPHANED_IN_DB.value] == 1


def test_attribute_mismatch_detected_on_custom_attribute_change():
    """A custom attribute changing out-of-band, with email/username
    unchanged, must still be detected -- this is the case
    COMPARED_FIELDS alone (email, username) can't catch."""
    cognito_users = [cognito_user("sub-1", "a@example.com", "alice", extra_attrs={"custom:role": "admin"})]
    db_users = [db_user("sub-1", "a@example.com", "alice", attributes={"sub": "sub-1", "email": "a@example.com", "custom:role": "member"})]

    drift = find_drift(cognito_users, db_users)

    assert len(drift) == 1
    assert drift[0].drift_type == DriftType.ATTRIBUTE_MISMATCH
    assert drift[0].mismatched_fields == ["attributes"]


def test_users_missing_sub_are_ignored():
    """Defensive: a malformed Cognito record with no sub attribute
    should never crash reconciliation -- it should just be skipped,
    since we have no key to match it against the DB."""
    cognito_users = [{"Username": "ghost", "Attributes": [{"Name": "email", "Value": "x@example.com"}]}]
    db_users = []

    drift = find_drift(cognito_users, db_users)

    assert drift == []
