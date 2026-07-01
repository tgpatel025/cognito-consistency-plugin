# Alerting infrastructure.
#
# Two independent alarm paths, because they catch different failure modes:
#
# 1. CRITICAL log alarm (fast, rare, severe)
#    Fires when a Lambda handler logs CRITICAL -- meaning both the primary
#    sync AND the dead-letter/audit fallback failed (see
#    src/lambdas/*/handler.py). This is the "we lost an event entirely"
#    case and should page someone immediately, since there is no database
#    record of what happened; the only trace is this log line.
#
# 2. Drift count alarm (slower, more common, lower severity per-incident)
#    Fires when the scheduled reconciler's DriftCount metric exceeds a
#    threshold. This catches the more common case: individual sync
#    failures that WERE recorded (dead-letter table has them) but are
#    accumulating faster than they're being replayed, or drift from
#    causes other than sync failures (e.g. direct Cognito admin edits).

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# 1. CRITICAL log alarm
# ---------------------------------------------------------------------------
# Scans each sync Lambda's log group for the word CRITICAL and turns
# matches into a CloudWatch metric. logger.critical() in the handlers is
# only reached when both the primary sync and the fallback dead-letter/
# audit writes fail -- see the nested try/except in handler.py.

resource "aws_cloudwatch_log_metric_filter" "post_confirmation_critical" {
  name           = "${var.project_name}-post-confirmation-critical"
  log_group_name = "/aws/lambda/${aws_lambda_function.post_confirmation.function_name}"
  pattern        = "CRITICAL"

  metric_transformation {
    name      = "CriticalFailures"
    namespace = "CognitoConsistencyPlatform"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "post_authentication_critical" {
  name           = "${var.project_name}-post-authentication-critical"
  log_group_name = "/aws/lambda/${aws_lambda_function.post_authentication.function_name}"
  pattern        = "CRITICAL"

  metric_transformation {
    name      = "CriticalFailures"
    namespace = "CognitoConsistencyPlatform"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "critical_failures" {
  alarm_name          = "${var.project_name}-critical-sync-failure"
  alarm_description   = "A sync event was lost entirely: both the primary sync and the dead-letter/audit fallback failed. No database record of this event exists. Investigate Postgres connectivity immediately."
  namespace           = "CognitoConsistencyPlatform"
  metric_name         = "CriticalFailures"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# 2. Drift accumulation alarm
# ---------------------------------------------------------------------------
# Watches the TOTAL drift count published by the scheduled reconciler
# (src/reconciler/scheduled_handler.py::publish_drift_metrics). Fires if
# drift stays above threshold across evaluation periods, which filters
# out normal transient blips (e.g. a user mid-signup at the exact moment
# the reconciler runs) while still catching sustained drift growth.

resource "aws_cloudwatch_metric_alarm" "drift_accumulation" {
  alarm_name          = "${var.project_name}-drift-accumulation"
  alarm_description   = "Cognito <-> database drift has exceeded ${var.drift_alarm_threshold} records across ${var.drift_alarm_evaluation_periods} consecutive reconciler runs. Run 'python -m reconciler.run --fix' after reviewing the diff, or investigate why sync is failing."
  namespace           = "CognitoConsistencyPlatform"
  metric_name         = "DriftCount"
  dimensions          = { DriftType = "TOTAL" }
  statistic           = "Maximum"
  period              = 900 # matches the 15-minute reconciler schedule
  evaluation_periods  = var.drift_alarm_evaluation_periods
  threshold           = var.drift_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# IAM: allow the reconciler Lambda to publish custom metrics
# ---------------------------------------------------------------------------
resource "aws_iam_role_policy" "reconciler_cloudwatch_metrics" {
  name = "${var.project_name}-cloudwatch-metrics"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["cloudwatch:PutMetricData"]
      Resource = "*" # PutMetricData does not support resource-level permissions
    }]
  })
}
