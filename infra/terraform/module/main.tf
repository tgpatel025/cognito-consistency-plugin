# Cognito Consistency Plugin -- reusable module
#
# Creates ONLY:
#   - 3 Lambda functions (post_confirmation, post_authentication, reconciler)
#   - IAM roles/policies scoped per-function to least privilege
#   - Cognito trigger permissions (optionally; see attach_* variables)
#   - EventBridge schedule for the reconciler
#   - CloudWatch alarms + SNS topic for alerting
#
# Does NOT create: a Cognito User Pool, an RDS/database instance, a VPC,
# or any network resources. Those are expected to already exist in the
# consuming account -- see variables.tf for exactly what's expected as
# input, and docs/extending-the-repository.md plus this module's own
# README.md (at the repo root) for adoption guidance.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

locals {
  lambda_env = merge(
    {
      USER_POOL_ID     = var.cognito_user_pool_id
      REPOSITORY_CLASS = var.repository_class
    },
    var.db_secret_arn != "" ? { DB_SECRET_ARN = var.db_secret_arn } : {}
  )
}

# ---------------------------------------------------------------------------
# Lambda: Post Confirmation
# ---------------------------------------------------------------------------
data "archive_file" "post_confirmation" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src"
  output_path = "${path.module}/build/post_confirmation.zip"
}

resource "aws_lambda_function" "post_confirmation" {
  function_name    = "${var.project_name}-post-confirmation"
  filename         = data.archive_file.post_confirmation.output_path
  source_code_hash = data.archive_file.post_confirmation.output_base64sha256
  handler          = "lambdas.post_confirmation.handler.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout_seconds
  role             = aws_iam_role.post_confirmation.arn
  tags             = var.tags

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  environment {
    variables = local.lambda_env
  }
}

resource "aws_lambda_permission" "allow_cognito_post_confirmation" {
  count         = var.attach_post_confirmation_trigger ? 1 : 0
  statement_id  = "AllowCognitoInvokePostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.post_confirmation.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = var.cognito_user_pool_arn
}

# ---------------------------------------------------------------------------
# Lambda: Post Authentication
# ---------------------------------------------------------------------------
data "archive_file" "post_authentication" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src"
  output_path = "${path.module}/build/post_authentication.zip"
}

resource "aws_lambda_function" "post_authentication" {
  function_name    = "${var.project_name}-post-authentication"
  filename         = data.archive_file.post_authentication.output_path
  source_code_hash = data.archive_file.post_authentication.output_base64sha256
  handler          = "lambdas.post_authentication.handler.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout_seconds
  role             = aws_iam_role.post_authentication.arn
  tags             = var.tags

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  environment {
    variables = local.lambda_env
  }
}

resource "aws_lambda_permission" "allow_cognito_post_auth" {
  count         = var.attach_post_authentication_trigger ? 1 : 0
  statement_id  = "AllowCognitoInvokePostAuth"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.post_authentication.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = var.cognito_user_pool_arn
}

# ---------------------------------------------------------------------------
# Lambda: Reconciler (scheduled)
# ---------------------------------------------------------------------------
data "archive_file" "reconciler" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src"
  output_path = "${path.module}/build/reconciler.zip"
}

resource "aws_lambda_function" "reconciler" {
  function_name    = "${var.project_name}-reconciler"
  filename         = data.archive_file.reconciler.output_path
  source_code_hash = data.archive_file.reconciler.output_base64sha256
  handler          = "reconciler.scheduled_handler.handler"
  runtime          = "python3.12"
  timeout          = var.reconciler_timeout_seconds
  role             = aws_iam_role.reconciler.arn
  tags             = var.tags

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  environment {
    variables = local.lambda_env
  }
}

resource "aws_cloudwatch_event_rule" "reconciler_schedule" {
  name                = "${var.project_name}-reconciler-schedule"
  schedule_expression = var.reconciler_schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "reconciler_target" {
  rule = aws_cloudwatch_event_rule.reconciler_schedule.name
  arn  = aws_lambda_function.reconciler.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconciler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconciler_schedule.arn
}
