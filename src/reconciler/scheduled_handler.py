"""
Entry point used when the reconciler runs as a scheduled Lambda
(triggered by EventBridge every 15 minutes) rather than as a CLI.

Kept as a thin wrapper around run.py's logic so the same reconciliation
code path is used whether it's invoked locally, in CI, or in AWS.
"""

import os
import logging

from reconciler.run import fetch_all_cognito_users
from common.db import get_all_app_users
from reconciler.drift import find_drift, summarize

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    user_pool_id = os.environ["USER_POOL_ID"]

    cognito_users = fetch_all_cognito_users(user_pool_id)
    db_users = get_all_app_users()

    drift_records = find_drift(cognito_users, db_users)
    summary = summarize(drift_records)

    logger.info("Scheduled reconciliation summary: %s", summary)

    # Note: this scheduled path deliberately only *reports* (via CloudWatch
    # Logs / could be wired to SNS or EventBridge for alerting). Auto-fixing
    # on every scheduled run without operator visibility would defeat the
    # purpose of having an auditable reconciliation trail. Use the CLI's
    # --fix flag for an explicit, operator-initiated repair.
    return {"summary": summary, "drift_count": len(drift_records)}
