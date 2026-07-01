"""
Replay failed sync events from the sync_dead_letters table.

This is the "Replay & Recovery" capability: when a Lambda trigger fails
to write to Postgres (DB outage, transient network error, schema
mismatch), the event is captured in sync_dead_letters rather than lost.
This script retries those events.

Two categories of dead letter behave very differently on replay:
  - Transient (DB was briefly down): replaying succeeds as soon as the
    DB is back, no special handling needed.
  - Permanent (the payload itself is bad -- e.g. a null email hitting a
    NOT NULL constraint): replaying will fail identically every time,
    forever, unless something intervenes. Without a retry limit, this
    becomes a silent "poison pill" that fails quietly on every scheduled
    replay run indefinitely.

To distinguish these, every failed replay attempt increments
retry_count and records last_error. Entries exceeding
MAX_RETRY_ATTEMPTS are skipped by default (still visible via --report)
rather than retried forever.

Usage:
    python -m reconciler.replay              # replay all eligible unreplayed entries
    python -m reconciler.replay --dry-run    # show what would be replayed
    python -m reconciler.replay --report     # show stuck (max-retries-exceeded) entries
"""

import argparse
import logging
from datetime import datetime, timezone

from common.db import db_cursor, upsert_user

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)

MAX_RETRY_ATTEMPTS = 5


def fetch_unreplayed(include_stuck=False):
    """By default, excludes entries that have already exceeded
    MAX_RETRY_ATTEMPTS -- these are treated as poison pills that need
    manual intervention, not automatic retry. Pass include_stuck=True
    to fetch everything regardless of retry_count (used by --report)."""
    query = "SELECT id, cognito_sub, payload, retry_count FROM sync_dead_letters WHERE replayed = false"
    if not include_stuck:
        query += " AND retry_count < %s"
    query += " ORDER BY occurred_at"

    with db_cursor(commit=False) as cur:
        if include_stuck:
            cur.execute(query)
        else:
            cur.execute(query, (MAX_RETRY_ATTEMPTS,))
        return cur.fetchall()


def fetch_stuck():
    """Entries that have exceeded the retry limit and are not being
    automatically retried. These need a human to look at last_error and
    either fix the underlying data/schema issue and reset retry_count,
    or resolve it another way (e.g. delete the Cognito user)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, cognito_sub, retry_count, last_error, occurred_at, last_attempted_at
            FROM sync_dead_letters
            WHERE replayed = false AND retry_count >= %s
            ORDER BY occurred_at
            """,
            (MAX_RETRY_ATTEMPTS,),
        )
        return cur.fetchall()


def mark_replayed(dead_letter_id):
    with db_cursor() as cur:
        cur.execute(
            "UPDATE sync_dead_letters SET replayed = true, replayed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), dead_letter_id),
        )


def record_failed_attempt(dead_letter_id, error):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE sync_dead_letters
            SET retry_count = retry_count + 1,
                last_error = %s,
                last_attempted_at = %s
            WHERE id = %s
            """,
            (str(error), datetime.now(timezone.utc), dead_letter_id),
        )


def replay_all(dry_run=False):
    entries = fetch_unreplayed()
    logger.info("Found %d unreplayed dead letter(s) eligible for retry (retry_count < %d)",
                len(entries), MAX_RETRY_ATTEMPTS)

    replayed, failed = 0, 0
    for entry in entries:
        payload = entry["payload"]
        if dry_run:
            logger.info("[dry-run] would replay cognito_sub=%s (attempt %d)",
                        entry["cognito_sub"], entry["retry_count"] + 1)
            continue
        try:
            upsert_user(
                cognito_sub=entry["cognito_sub"],
                email=payload.get("email"),
                username=payload.get("username") or payload.get("cognito:username"),
                attributes=payload,
                event_source="replay",
            )
            mark_replayed(entry["id"])
            replayed += 1
        except Exception as exc:
            logger.error("Replay failed for %s (attempt %d): %s",
                         entry["cognito_sub"], entry["retry_count"] + 1, exc)
            record_failed_attempt(entry["id"], exc)
            failed += 1

    logger.info("Replay complete: %d succeeded, %d failed", replayed, failed)
    return replayed, failed


def print_stuck_report():
    stuck = fetch_stuck()
    if not stuck:
        print("No stuck dead letters (all entries are either replayed or within retry limit).")
        return

    print(f"\n{len(stuck)} dead letter(s) exceeded {MAX_RETRY_ATTEMPTS} retry attempts and are no longer auto-retried:\n")
    for entry in stuck:
        print(f"  id={entry['id']} cognito_sub={entry['cognito_sub']} "
              f"retries={entry['retry_count']} last_attempted={entry['last_attempted_at']}")
        print(f"    last_error: {entry['last_error']}\n")
    print("These require manual investigation. After fixing the underlying issue, "
          "reset retry_count to 0 for the affected row(s) to make them eligible for replay again.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="Show dead letters stuck past the retry limit")
    args = parser.parse_args()

    if args.report:
        print_stuck_report()
    else:
        replay_all(dry_run=args.dry_run)
