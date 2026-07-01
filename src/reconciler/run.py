"""
Reconciler runner -- the piece that would run on a schedule (e.g. every
15 minutes via EventBridge Scheduler -> Lambda, or as a CLI for manual
runs).

Usage:
    python -m reconciler.run --user-pool-id us-east-1_XXXXXXX [--fix] [--json]

By default this only *reports* drift (read-only). Pass --fix to actually
apply repairs:
  - MISSING_IN_DB      -> insert the missing row from Cognito data
  - ATTRIBUTE_MISMATCH -> overwrite DB fields with Cognito's values
                          (Cognito is treated as the source of truth for
                          identity attributes -- see docs/architecture.md
                          for why this direction was chosen)
  - ORPHANED_IN_DB      -> never auto-deleted. Orphans are reported only;
                          deleting business data automatically is judged
                          too risky for an automated pass. A human/ticket
                          should confirm before deletion.
"""

import argparse
import json
import logging
import os
import sys

import boto3

from common.db import get_all_app_users, upsert_user, log_sync_event
from reconciler.drift import find_drift, summarize, DriftType

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)


def fetch_all_cognito_users(user_pool_id: str, endpoint_url: str = None) -> list:
    client = boto3.client("cognito-idp", endpoint_url=endpoint_url)
    users = []
    paginator_kwargs = {"UserPoolId": user_pool_id}
    while True:
        resp = client.list_users(**paginator_kwargs)
        users.extend(resp.get("Users", []))
        token = resp.get("PaginationToken")
        if not token:
            break
        paginator_kwargs["PaginationToken"] = token
    return users


def apply_fix(record):
    if record.drift_type == DriftType.MISSING_IN_DB:
        data = record.cognito_data
        upsert_user(
            cognito_sub=data["cognito_sub"],
            email=data.get("email"),
            username=data.get("username"),
            attributes=data.get("attributes", {}),
            event_source="reconciler",
        )
        return "inserted missing row"

    if record.drift_type == DriftType.ATTRIBUTE_MISMATCH:
        data = record.cognito_data
        upsert_user(
            cognito_sub=data["cognito_sub"],
            email=data.get("email"),
            username=data.get("username"),
            attributes=data.get("attributes", {}),
            event_source="reconciler",
        )
        return f"corrected fields: {', '.join(record.mismatched_fields)}"

    if record.drift_type == DriftType.ORPHANED_IN_DB:
        log_sync_event(
            cognito_sub=record.cognito_sub,
            event_source="reconciler",
            status="flagged",
            detail="orphaned row -- requires manual review, not auto-deleted",
        )
        return "flagged for manual review (not deleted)"

    return "no action"


def main():
    parser = argparse.ArgumentParser(description="Cognito <-> DB reconciliation")
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--endpoint-url", default=os.environ.get("AWS_ENDPOINT_URL"),
                         help="Use for LocalStack, e.g. http://localhost:4566")
    parser.add_argument("--fix", action="store_true", help="Apply repairs, not just report")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    cognito_users = fetch_all_cognito_users(args.user_pool_id, args.endpoint_url)
    db_users = get_all_app_users()

    drift_records = find_drift(cognito_users, db_users)
    summary = summarize(drift_records)

    results = []
    for record in drift_records:
        entry = {
            "cognito_sub": record.cognito_sub,
            "drift_type": record.drift_type.value,
            "mismatched_fields": record.mismatched_fields,
        }
        if args.fix:
            entry["action_taken"] = apply_fix(record)
        results.append(entry)

    output = {"summary": summary, "records": results}

    if args.json:
        print(json.dumps(output, indent=2, default=str))
    else:
        print(f"\nReconciliation summary: {summary}\n")
        for entry in results:
            line = f"  [{entry['drift_type']}] {entry['cognito_sub']}"
            if entry["mismatched_fields"]:
                line += f" (fields: {', '.join(entry['mismatched_fields'])})"
            if "action_taken" in entry:
                line += f" -> {entry['action_taken']}"
            print(line)

    return 0 if summary["total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
