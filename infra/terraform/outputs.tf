output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.this.id
}

output "db_endpoint" {
  value = aws_db_instance.postgres.address
}

output "reconciler_function_name" {
  value = aws_lambda_function.reconciler.function_name
}

output "alerts_topic_arn" {
  description = "SNS topic ARN for critical failure and drift alarms. Subscribe additional endpoints (Slack, PagerDuty, etc.) here."
  value       = aws_sns_topic.alerts.arn
}
