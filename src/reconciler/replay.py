"""
Replay failed sync events from the dead-letter store.

Two kinds of dead letter:
  - Transient (DB was down): replay succeeds once it's back.
  - Permanent (bad payload, e.g. null email vs NOT NULL): fails
    identically forever -- a poison pill without a retry limit.

So every failed replay bumps retry_count + records last_error; entries
past MAX_RETRY_ATTEMPTS are skipped (visible via --report), not retried
forever. Storage via SyncService (docs/extending-the-repository.md).

Usage:
    python -m reconciler.replay              # replay eligible entries
    python -m reconciler.replay --dry-run    # show what would be replayed
    python -m reconciler.replay --report     # show stuck entries
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
        attributes = payload.get("attributes", {})
        if dry_run:
            logger.info("[dry-run] would replay cognito_sub=%s (attempt %d)",
                        entry["cognito_sub"], entry["retry_count"] + 1)
            continue
        try:
            sync_service.sync_user(
                cognito_sub=entry["cognito_sub"],
                email=attributes.get("email"),
                username=payload.get("username"),
                attributes=attributes,
                event_source="replay",
            )
            sync_service.mark_dead_letter_replayed(entry["id"])
            replayed += 1
        except Exception as exc:
            # Type only -- full message (may embed PII) goes to last_error
            # via record_dead_letter_failure, not CloudWatch.
            logger.error("Replay failed for %s (attempt %d): %s",
                         entry["cognito_sub"], entry["retry_count"] + 1, type(exc).__name__)
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
