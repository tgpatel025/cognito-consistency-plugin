"""
Replay failed sync events from the dead-letter store.

This is the "Replay & Recovery" capability: when a Lambda trigger fails
to write to the database (outage, transient network error, schema
mismatch), the event is captured as a dead letter rather than lost.
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

Storage: depends on SyncService, not on any specific database directly
-- see docs/extending-the-repository.md for how to point this at your
own schema.

Usage:
    python -m reconciler.replay              # replay all eligible unreplayed entries
    python -m reconciler.replay --dry-run    # show what would be replayed
    python -m reconciler.replay --report     # show stuck (max-retries-exceeded) entries
"""

import argparse
import logging

from common.service_factory import build_sync_service

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)

MAX_RETRY_ATTEMPTS = 5


def replay_all(sync_service, dry_run=False):
    entries = sync_service.fetch_unreplayed_dead_letters(MAX_RETRY_ATTEMPTS)
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
            sync_service.sync_user(
                cognito_sub=entry["cognito_sub"],
                email=payload.get("email"),
                username=payload.get("username") or payload.get("cognito:username"),
                attributes=payload,
                event_source="replay",
            )
            sync_service.mark_dead_letter_replayed(entry["id"])
            replayed += 1
        except Exception as exc:
            logger.error("Replay failed for %s (attempt %d): %s",
                         entry["cognito_sub"], entry["retry_count"] + 1, exc)
            sync_service.record_dead_letter_failure(entry["id"], exc)
            failed += 1

    logger.info("Replay complete: %d succeeded, %d failed", replayed, failed)
    return replayed, failed


def print_stuck_report(sync_service):
    stuck = sync_service.fetch_stuck_dead_letters(MAX_RETRY_ATTEMPTS)
    if not stuck:
        print("No stuck dead letters (all entries are either replayed or within retry limit).")
        return

    print(f"\n{len(stuck)} dead letter(s) exceeded {MAX_RETRY_ATTEMPTS} retry attempts and are no longer auto-retried:\n")
    for entry in stuck:
        print(f"  id={entry['id']} cognito_sub={entry['cognito_sub']} "
              f"retries={entry['retry_count']} last_attempted={entry.get('last_attempted_at')}")
        print(f"    last_error: {entry.get('last_error')}\n")
    print("These require manual investigation. After fixing the underlying issue, "
          "reset retry_count to 0 for the affected row(s) to make them eligible for replay again.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="Show dead letters stuck past the retry limit")
    args = parser.parse_args()

    service = build_sync_service()

    if args.report:
        print_stuck_report(service)
    else:
        replay_all(service, dry_run=args.dry_run)
