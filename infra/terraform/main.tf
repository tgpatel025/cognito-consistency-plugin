terraform {
  required_version = ">= 1.5"
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

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Cognito User Pool
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool" "this" {
  name = "${var.project_name}-user-pool"

  auto_verified_attributes = ["email"]

  lambda_config {
    post_confirmation   = aws_lambda_function.post_confirmation.arn
    post_authentication = aws_lambda_function.post_authentication.arn
  }
}

resource "aws_cognito_user_pool_client" "this" {
  name         = "${var.project_name}-client"
  user_pool_id = aws_cognito_user_pool.this.id
}

# ---------------------------------------------------------------------------
# Networking-free RDS Postgres (demo: publicly accessible for simplicity).
# Production would place this in a private subnet with a VPC Lambda config.
# ---------------------------------------------------------------------------
resource "aws_db_instance" "postgres" {
  identifier             = "${var.project_name}-db"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  db_name                = "identity_platform"
  username               = var.db_username
  password               = var.db_password
  publicly_accessible    = true
  skip_final_snapshot    = true
  apply_immediately      = true
}

# ---------------------------------------------------------------------------
# Lambda: Post Confirmation
# ---------------------------------------------------------------------------
data "archive_file" "post_confirmation" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/build/post_confirmation.zip"
}

resource "aws_lambda_function" "post_confirmation" {
  function_name    = "${var.project_name}-post-confirmation"
  filename         = data.archive_file.post_confirmation.output_path
  source_code_hash = data.archive_file.post_confirmation.output_base64sha256
  handler          = "lambdas.post_confirmation.handler.handler"
  runtime          = "python3.12"
  timeout          = 5
  role             = aws_iam_role.lambda_exec.arn

  environment {
    variables = {
      DB_HOST     = aws_db_instance.postgres.address
      DB_PORT     = tostring(aws_db_instance.postgres.port)
      DB_NAME     = aws_db_instance.postgres.db_name
      DB_USER     = var.db_username
      DB_PASSWORD = var.db_password
    }
  }
}

resource "aws_lambda_permission" "allow_cognito_post_confirmation" {
  statement_id  = "AllowCognitoInvokePostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.post_confirmation.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# ---------------------------------------------------------------------------
# Lambda: Post Authentication
# ---------------------------------------------------------------------------
data "archive_file" "post_authentication" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/build/post_authentication.zip"
}

resource "aws_lambda_function" "post_authentication" {
  function_name    = "${var.project_name}-post-authentication"
  filename         = data.archive_file.post_authentication.output_path
  source_code_hash = data.archive_file.post_authentication.output_base64sha256
  handler          = "lambdas.post_authentication.handler.handler"
  runtime          = "python3.12"
  timeout          = 5
  role             = aws_iam_role.lambda_exec.arn

  environment {
    variables = {
      DB_HOST     = aws_db_instance.postgres.address
      DB_PORT     = tostring(aws_db_instance.postgres.port)
      DB_NAME     = aws_db_instance.postgres.db_name
      DB_USER     = var.db_username
      DB_PASSWORD = var.db_password
    }
  }
}

resource "aws_lambda_permission" "allow_cognito_post_auth" {
  statement_id  = "AllowCognitoInvokePostAuth"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.post_authentication.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# ---------------------------------------------------------------------------
# Reconciler: scheduled Lambda run every 15 minutes
# ---------------------------------------------------------------------------
data "archive_file" "reconciler" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/build/reconciler.zip"
}

resource "aws_lambda_function" "reconciler" {
  function_name    = "${var.project_name}-reconciler"
  filename         = data.archive_file.reconciler.output_path
  source_code_hash = data.archive_file.reconciler.output_base64sha256
  handler          = "reconciler.scheduled_handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  role             = aws_iam_role.lambda_exec.arn

  environment {
    variables = {
      DB_HOST      = aws_db_instance.postgres.address
      DB_PORT      = tostring(aws_db_instance.postgres.port)
      DB_NAME      = aws_db_instance.postgres.db_name
      DB_USER      = var.db_username
      DB_PASSWORD  = var.db_password
      USER_POOL_ID = aws_cognito_user_pool.this.id
    }
  }
}

resource "aws_cloudwatch_event_rule" "reconciler_schedule" {
  name                = "${var.project_name}-reconciler-schedule"
  schedule_expression = "rate(15 minutes)"
}

resource "aws_cloudwatch_event_target" "reconciler_target" {
  rule      = aws_cloudwatch_event_rule.reconciler_schedule.name
  arn       = aws_lambda_function.reconciler.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconciler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconciler_schedule.arn
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda_exec" {
  name = "${var.project_name}-lambda-exec-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "cognito_read" {
  name = "${var.project_name}-cognito-read"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["cognito-idp:ListUsers"]
      Resource = aws_cognito_user_pool.this.arn
    }]
  })
}
