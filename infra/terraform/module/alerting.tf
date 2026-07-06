# Alerting. See docs/architecture.md ("Silent failures are alarmable")
# in the repo root for the two-alarm design rationale.
#
# Supports plugging into an existing SNS topic (existing_alerts_topic_arn)
# for teams that already have an incident-management/on-call topic,
# rather than forcing a new one.

locals {
  alerts_topic_arn = var.existing_alerts_topic_arn != "" ? var.existing_alerts_topic_arn : aws_sns_topic.alerts[0].arn
}

resource "aws_sns_topic" "alerts" {
  count             = var.existing_alerts_topic_arn == "" ? 1 : 0
  name              = "${var.project_name}-alerts"
  kms_master_key_id = var.sns_kms_key_id
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email != "" && var.existing_alerts_topic_arn == "" ? 1 : 0
  topic_arn = local.alerts_topic_arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# 1. CRITICAL log alarm (per sync Lambda)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_metric_filter" "post_confirmation_critical" {
  name           = "${var.project_name}-post-confirmation-critical"
  log_group_name = "/aws/lambda/${aws_lambda_function.post_confirmation.function_name}"
  pattern        = "CRITICAL"

  metric_transformation {
    name      = "CriticalFailures"
    namespace = "CognitoConsistencyPlugin"
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
    namespace = "CognitoConsistencyPlugin"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "critical_failures" {
  alarm_name          = "${var.project_name}-critical-sync-failure"
  alarm_description   = "A sync event was lost entirely: both the primary sync and the dead-letter/audit fallback failed. No database record of this event exists. Investigate database connectivity immediately."
  namespace           = "CognitoConsistencyPlugin"
  metric_name         = "CriticalFailures"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [local.alerts_topic_arn]
  ok_actions    = [local.alerts_topic_arn]
  tags          = var.tags
}

# ---------------------------------------------------------------------------
# 2. Drift accumulation alarm
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "drift_accumulation" {
  alarm_name          = "${var.project_name}-drift-accumulation"
  alarm_description   = "Cognito <-> database drift has exceeded ${var.drift_alarm_threshold} records across ${var.drift_alarm_evaluation_periods} consecutive reconciler runs. Run 'python -m reconciler.run --fix' after reviewing the diff, or investigate why sync is failing."
  namespace           = "CognitoConsistencyPlugin"
  metric_name         = "DriftCount"
  dimensions          = { DriftType = "TOTAL" }
  statistic           = "Maximum"
  period              = 900
  evaluation_periods  = var.drift_alarm_evaluation_periods
  threshold           = var.drift_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [local.alerts_topic_arn]
  ok_actions    = [local.alerts_topic_arn]
  tags          = var.tags
}
