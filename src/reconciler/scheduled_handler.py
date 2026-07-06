"""
Reconciler as a scheduled Lambda (EventBridge) -- thin wrapper around
run.py so local/CI/AWS all share one code path. Storage via SyncService,
never a specific database (docs/extending-the-repository.md).

Drift counts publish as a CloudWatch metric (CognitoConsistencyPlugin /
DriftCount, dimensioned by drift type) so drift is *alarmable* instead
of something a human remembers to grep logs for -- alerting.tf watches
it. Metric over log-parsing: structured, cheap, graphable time series.
"""

import os
import logging

import boto3

from reconciler.run import fetch_all_cognito_users
from common.service_factory import build_sync_service
from reconciler.drift import find_drift, summarize, DriftType

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "CognitoConsistencyPlugin"


def publish_drift_metrics(summary: dict):
    """One datapoint per drift type + a total, so CloudWatch can alarm
    on either. Client created here (not module level) so importing this
    module without an AWS region configured doesn't NoRegionError."""
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
