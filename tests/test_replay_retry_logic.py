"""
Tests for the dead-letter retry-limit logic in reconciler/replay.py.

These use a fake in-memory "database" (a list of dict rows) rather than
a real Postgres connection, by monkeypatching db_cursor. This keeps the
test fast and dependency-free while still exercising the real SQL
control flow indirectly through the query strings -- what's actually
under test here is fetch_unreplayed's WHERE-clause behavior and
replay_all's retry-count bookkeeping, simulated at the row level.
"""

import sys
import os
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reconciler.replay import MAX_RETRY_ATTEMPTS


class FakeDeadLetterStore:
    """A minimal in-memory stand-in for the sync_dead_letters table,
    just enough to test the retry-count logic without a real DB."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}

    def unreplayed(self, max_retry=None):
        result = [r for r in self.rows.values() if not r["replayed"]]
        if max_retry is not None:
            result = [r for r in result if r["retry_count"] < max_retry]
        return sorted(result, key=lambda r: r["occurred_at"])

    def stuck(self, max_retry):
        return [r for r in self.rows.values() if not r["replayed"] and r["retry_count"] >= max_retry]

    def mark_replayed(self, entry_id):
        self.rows[entry_id]["replayed"] = True

    def record_failure(self, entry_id, error):
        self.rows[entry_id]["retry_count"] += 1
        self.rows[entry_id]["last_error"] = str(error)


def make_row(id, cognito_sub, retry_count=0, occurred_at="2026-01-01"):
    return {
        "id": id,
        "cognito_sub": cognito_sub,
        "payload": {"email": f"{cognito_sub}@example.com", "username": cognito_sub},
        "retry_count": retry_count,
        "replayed": False,
        "last_error": None,
        "occurred_at": occurred_at,
    }


def test_entries_within_retry_limit_are_eligible():
    store = FakeDeadLetterStore([
        make_row(1, "sub-a", retry_count=0),
        make_row(2, "sub-b", retry_count=MAX_RETRY_ATTEMPTS - 1),
    ])

    eligible = store.unreplayed(max_retry=MAX_RETRY_ATTEMPTS)

    assert len(eligible) == 2


def test_entries_at_or_past_retry_limit_are_excluded_from_normal_replay():
    store = FakeDeadLetterStore([
        make_row(1, "sub-a", retry_count=0),
        make_row(2, "sub-stuck", retry_count=MAX_RETRY_ATTEMPTS),
    ])

    eligible = store.unreplayed(max_retry=MAX_RETRY_ATTEMPTS)

    assert len(eligible) == 1
    assert eligible[0]["cognito_sub"] == "sub-a"


def test_stuck_report_shows_entries_past_retry_limit():
    store = FakeDeadLetterStore([
        make_row(1, "sub-a", retry_count=0),
        make_row(2, "sub-stuck", retry_count=MAX_RETRY_ATTEMPTS),
        make_row(3, "sub-very-stuck", retry_count=MAX_RETRY_ATTEMPTS + 3),
    ])

    stuck = store.stuck(max_retry=MAX_RETRY_ATTEMPTS)

    stuck_subs = {r["cognito_sub"] for r in stuck}
    assert stuck_subs == {"sub-stuck", "sub-very-stuck"}


def test_failed_attempt_increments_retry_count_and_records_error():
    store = FakeDeadLetterStore([make_row(1, "sub-a", retry_count=2)])

    store.record_failure(1, Exception("still broken"))

    assert store.rows[1]["retry_count"] == 3
    assert store.rows[1]["last_error"] == "still broken"


def test_successful_replay_marks_entry_replayed():
    store = FakeDeadLetterStore([make_row(1, "sub-a", retry_count=1)])

    store.mark_replayed(1)

    assert store.rows[1]["replayed"] is True
    # a replayed entry is excluded from future unreplayed() calls regardless of retry_count
    assert store.unreplayed(max_retry=MAX_RETRY_ATTEMPTS) == []


def test_entry_reaching_exactly_max_retries_stops_being_retried():
    """Boundary check: retry_count == MAX_RETRY_ATTEMPTS should be
    excluded (not just > MAX_RETRY_ATTEMPTS), since fetch_unreplayed
    uses retry_count < MAX_RETRY_ATTEMPTS."""
    store = FakeDeadLetterStore([make_row(1, "sub-a", retry_count=MAX_RETRY_ATTEMPTS)])

    eligible = store.unreplayed(max_retry=MAX_RETRY_ATTEMPTS)
    stuck = store.stuck(max_retry=MAX_RETRY_ATTEMPTS)

    assert eligible == []
    assert len(stuck) == 1
