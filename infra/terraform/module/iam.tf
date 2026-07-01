# Per-function IAM roles.
#
# Previously (see infra/terraform/main.tf, the standalone example) all
# three Lambdas shared one role with the union of everyone's permissions
# -- meaning post_confirmation could call cognito-idp:ListUsers even
# though it has no legitimate reason to. Each function gets its own role
# here, granted only what it specifically does:
#
#   post_confirmation / post_authentication:
#     - basic Lambda execution (CloudWatch Logs)
#     - secretsmanager:GetSecretValue on exactly db_secret_arn
#     - (VPC-attached Lambdas additionally need ENI permissions, added
#       conditionally below)
#
#   reconciler:
#     - everything the sync Lambdas get, PLUS
#     - cognito-idp:ListUsers scoped to exactly cognito_user_pool_arn
#     - cloudwatch:PutMetricData (required for PutMetricData, which does
#       not support resource-level scoping -- see AWS IAM reference)

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "read_db_secret" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.db_secret_arn]
  }
}

data "aws_iam_policy_document" "cognito_list_users" {
  statement {
    effect    = "Allow"
    actions   = ["cognito-idp:ListUsers"]
    resources = [var.cognito_user_pool_arn]
  }
}

data "aws_iam_policy_document" "put_metric_data" {
  statement {
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    # PutMetricData does not support resource-level permissions -- this
    # is an AWS API limitation, not a choice made here. Scoping is
    # instead enforced by which role has this statement at all: only
    # the reconciler role does.
    resources = ["*"]
  }
}

# ---------------------------------------------------------------------------
# post_confirmation role: logs + secret read only
# ---------------------------------------------------------------------------
resource "aws_iam_role" "post_confirmation" {
  name               = "${var.project_name}-post-confirmation-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "post_confirmation_basic" {
  role       = aws_iam_role.post_confirmation.name
  policy_arn = var.vpc_config != null ? "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole" : "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "post_confirmation_secret" {
  name   = "${var.project_name}-post-confirmation-secret"
  role   = aws_iam_role.post_confirmation.id
  policy = data.aws_iam_policy_document.read_db_secret.json
}

# ---------------------------------------------------------------------------
# post_authentication role: logs + secret read only (identical shape to
# post_confirmation, kept as a separate role rather than shared so the
# two functions' blast radius never silently grows together if one
# needs a new permission later)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "post_authentication" {
  name               = "${var.project_name}-post-authentication-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "post_authentication_basic" {
  role       = aws_iam_role.post_authentication.name
  policy_arn = var.vpc_config != null ? "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole" : "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "post_authentication_secret" {
  name   = "${var.project_name}-post-authentication-secret"
  role   = aws_iam_role.post_authentication.id
  policy = data.aws_iam_policy_document.read_db_secret.json
}

# ---------------------------------------------------------------------------
# reconciler role: logs + secret read + Cognito ListUsers + PutMetricData
# ---------------------------------------------------------------------------
resource "aws_iam_role" "reconciler" {
  name               = "${var.project_name}-reconciler-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "reconciler_basic" {
  role       = aws_iam_role.reconciler.name
  policy_arn = var.vpc_config != null ? "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole" : "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "reconciler_secret" {
  name   = "${var.project_name}-reconciler-secret"
  role   = aws_iam_role.reconciler.id
  policy = data.aws_iam_policy_document.read_db_secret.json
}

resource "aws_iam_role_policy" "reconciler_cognito" {
  name   = "${var.project_name}-reconciler-cognito"
  role   = aws_iam_role.reconciler.id
  policy = data.aws_iam_policy_document.cognito_list_users.json
}

resource "aws_iam_role_policy" "reconciler_metrics" {
  name   = "${var.project_name}-reconciler-metrics"
  role   = aws_iam_role.reconciler.id
  policy = data.aws_iam_policy_document.put_metric_data.json
}
