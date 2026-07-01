output "post_confirmation_function_arn" {
  value = aws_lambda_function.post_confirmation.arn
}

output "post_authentication_function_arn" {
  value = aws_lambda_function.post_authentication.arn
}

output "reconciler_function_arn" {
  value = aws_lambda_function.reconciler.arn
}

output "reconciler_function_name" {
  value = aws_lambda_function.reconciler.function_name
}

output "alerts_topic_arn" {
  description = "SNS topic ARN receiving alarms. Either the newly created topic or existing_alerts_topic_arn if you supplied one."
  value       = local.alerts_topic_arn
}

output "post_confirmation_role_arn" {
  description = "IAM role ARN for the post_confirmation Lambda -- useful if you need to grant it additional permissions for your own schema/extensions."
  value       = aws_iam_role.post_confirmation.arn
}

output "post_authentication_role_arn" {
  value = aws_iam_role.post_authentication.arn
}

output "reconciler_role_arn" {
  value = aws_iam_role.reconciler.arn
}
