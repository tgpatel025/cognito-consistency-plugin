"""
Replay failed sync events from the sync_dead_letters table.

This is the "Replay & Recovery" capability: when a Lambda trigger fails
to write to Postgres (DB outage, transient network error, schema
mismatch), the event is captured in sync_dead_letters rather than lost.
This script retries those events.

Usage:
    python -m reconciler.replay              # replay all unreplayed entries
    python -m reconciler.replay --dry-run    # show what would be replayed
"""

import argparse
import logging
from datetime import datetime, timezone

from common.db import db_cursor, upsert_user

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)


def fetch_unreplayed():
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, cognito_sub, payload FROM sync_dead_letters WHERE replayed = false ORDER BY occurred_at"
        )
        return cur.fetchall()


def mark_replayed(dead_letter_id):
    with db_cursor() as cur:
        cur.execute(
            "UPDATE sync_dead_letters SET replayed = true, replayed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), dead_letter_id),
        )


def replay_all(dry_run=False):
    entries = fetch_unreplayed()
    logger.info("Found %d unreplayed dead letter(s)", len(entries))

    replayed, failed = 0, 0
    for entry in entries:
        payload = entry["payload"]
        if dry_run:
            logger.info("[dry-run] would replay cognito_sub=%s", entry["cognito_sub"])
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
            logger.error("Replay failed for %s: %s", entry["cognito_sub"], exc)
            failed += 1

    logger.info("Replay complete: %d succeeded, %d failed", replayed, failed)
    return replayed, failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    replay_all(dry_run=args.dry_run)
