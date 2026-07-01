"""
Tests for the scheduled reconciler's CloudWatch metric publishing.

Mocks boto3.client so no real AWS call happens and no region needs to be
configured -- this also guards against a regression of the bug where
boto3.client("cloudwatch") was created at module import time and broke
any environment without AWS_DEFAULT_REGION set.
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reconciler.scheduled_handler import publish_drift_metrics, METRIC_NAMESPACE
from reconciler.drift import DriftType


def test_module_imports_without_aws_region_configured():
    """Regression test: this used to fail at import time because the
    CloudWatch client was created at module load, before any function
    was even called."""
    import importlib
    import reconciler.scheduled_handler as sched
    importlib.reload(sched)  # would raise NoRegionError if the bug returned


def test_publish_drift_metrics_sends_one_datapoint_per_drift_type_plus_total():
    summary = {
        DriftType.MISSING_IN_DB.value: 3,
        DriftType.ORPHANED_IN_DB.value: 1,
        DriftType.ATTRIBUTE_MISMATCH.value: 2,
        "total": 6,
    }

    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client) as mock_boto_client:
        publish_drift_metrics(summary)

    mock_boto_client.assert_called_once_with("cloudwatch")
    mock_client.put_metric_data.assert_called_once()

    call_kwargs = mock_client.put_metric_data.call_args.kwargs
    assert call_kwargs["Namespace"] == METRIC_NAMESPACE

    metric_data = call_kwargs["MetricData"]
    # one per DriftType member + one TOTAL
    assert len(metric_data) == len(DriftType) + 1

    values_by_dimension = {
        m["Dimensions"][0]["Value"]: m["Value"] for m in metric_data
    }
    assert values_by_dimension["MISSING_IN_DB"] == 3
    assert values_by_dimension["ORPHANED_IN_DB"] == 1
    assert values_by_dimension["ATTRIBUTE_MISMATCH"] == 2
    assert values_by_dimension["TOTAL"] == 6


def test_publish_drift_metrics_never_raises_if_cloudwatch_call_fails():
    """Publishing metrics is best-effort -- a CloudWatch outage must not
    break the reconciliation run itself."""
    summary = {t.value: 0 for t in DriftType}
    summary["total"] = 0

    mock_client = MagicMock()
    mock_client.put_metric_data.side_effect = Exception("cloudwatch unavailable")

    with patch("boto3.client", return_value=mock_client):
        publish_drift_metrics(summary)  # must not raise
