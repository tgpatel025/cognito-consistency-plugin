"""
Entry point used when the reconciler runs as a scheduled Lambda
(triggered by EventBridge every 15 minutes) rather than as a CLI.

Kept as a thin wrapper around run.py's logic so the same reconciliation
code path is used whether it's invoked locally, in CI, or in AWS.

Storage: depends on SyncService, not on any specific database directly
-- see docs/extending-the-repository.md.

Alerting
--------
Drift counts are published as a CloudWatch custom metric
(namespace: CognitoConsistencyPlatform, metric: DriftCount, broken down
by drift type via a dimension). This is what makes drift *alarmable*
rather than something a human has to remember to check logs for --
see infra/terraform/module/alerting.tf for the CloudWatch Alarm + SNS
topic that watches this metric.

A metric was chosen over parsing log lines because it's structured,
cheap, and gives you a real time series to graph (e.g. "drift count over
the last 7 days") rather than just a threshold trip.
"""

import os
import logging

import boto3

from reconciler.run import fetch_all_cognito_users
from common.service_factory import build_sync_service
from reconciler.drift import find_drift, summarize, DriftType

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "CognitoConsistencyPlatform"


def publish_drift_metrics(summary: dict):
    """Publish one metric datapoint per drift type plus a total, so
    CloudWatch can alarm on any specific type or on overall drift.

    The client is created here, not at module import time, so this
    module can be imported in local/test environments without an AWS
    region configured -- boto3.client() raises NoRegionError immediately
    if a region can't be resolved, and that would happen at import time
    for every caller if the client were module-level."""
    cloudwatch = boto3.client("cloudwatch")

    metric_data = [
        {
            "MetricName": "DriftCount",
            "Dimensions": [{"Name": "DriftType", "Value": drift_type.value}],
            "Value": summary.get(drift_type.value, 0),
            "Unit": "Count",
        }
        for drift_type in DriftType
    ]
    metric_data.append(
        {
            "MetricName": "DriftCount",
            "Dimensions": [{"Name": "DriftType", "Value": "TOTAL"}],
            "Value": summary["total"],
            "Unit": "Count",
        }
    )

    try:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)
    except Exception as exc:
        # Never let metric publishing failure break the reconciliation run
        # itself -- the run's own log output is still the fallback record.
        logger.error("Failed to publish CloudWatch metrics: %s", exc)


def handler(event, context):
    user_pool_id = os.environ["USER_POOL_ID"]

    sync_service = build_sync_service()

    cognito_users = fetch_all_cognito_users(user_pool_id, os.environ.get("AWS_ENDPOINT_URL"))
    db_users = sync_service.get_all_users()

    drift_records = find_drift(cognito_users, db_users)
    summary = summarize(drift_records)

    logger.info("Scheduled reconciliation summary: %s", summary)
    publish_drift_metrics(summary)

    # This scheduled path deliberately only *reports* -- the CloudWatch
    # Alarm defined in infra/terraform/module/alerting.tf is what turns
    # this metric into a notification. Auto-fixing on every scheduled
    # run without operator visibility would defeat the purpose of having
    # an auditable reconciliation trail. Use the CLI's --fix flag for an
    # explicit, operator-initiated repair.
    return {"summary": summary, "drift_count": len(drift_records)}
