# Input variables for the Cognito Consistency Platform module.
#
# Design principle: this module owns only the pieces this project
# actually invented -- the sync Lambdas, the reconciler, and the
# alerting that watches them. It does NOT create a Cognito User Pool,
# an RDS instance, a VPC, or IAM boundaries, because a developer
# adopting this almost certainly already has those, with their own
# schema, their own network topology, and their own security posture.
# Forcing them to adopt this module's opinions on those pieces would
# make it unusable as a drop-in addition to an existing stack.

variable "project_name" {
  description = "Prefix used for all resource names created by this module"
  type        = string
  default     = "cognito-consistency"
}

# ---------------------------------------------------------------------------
# Cognito integration (existing User Pool -- not created here)
# ---------------------------------------------------------------------------
variable "cognito_user_pool_arn" {
  description = "ARN of the existing Cognito User Pool to attach sync triggers and the reconciler's read permissions to."
  type        = string
}

variable "cognito_user_pool_id" {
  description = "ID of the existing Cognito User Pool (e.g. us-east-1_XXXXXXX). Used by the reconciler to call ListUsers."
  type        = string
}

variable "attach_post_confirmation_trigger" {
  description = "Whether to wire post_confirmation as this User Pool's Post Confirmation Lambda trigger. Set to false if you already have a Post Confirmation trigger and want to invoke this module's logic from within your own handler instead (see this module's README.md)."
  type        = bool
  default     = true
}

variable "attach_post_authentication_trigger" {
  description = "Whether to wire post_authentication as this User Pool's Post Authentication Lambda trigger. Same caveat as attach_post_confirmation_trigger."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Database connectivity (existing database -- not created here)
# ---------------------------------------------------------------------------
# This module has no opinion on your database or how your repository
# connects to it -- db_secret_arn is optional and only wires up IAM
# access to a Secrets Manager secret, which is one common way to store
# credentials but not the only one. If your repository doesn't use
# Secrets Manager (e.g. it uses DynamoDB via the Lambda's default IAM
# role, or reads credentials some other way), leave this empty and use
# additional_iam_policy_json for whatever access it actually needs.
variable "db_secret_arn" {
  description = "Optional: ARN of a Secrets Manager secret your repository reads for DB connection details. If set, this module's Lambdas are granted secretsmanager:GetSecretValue on exactly this ARN. Leave empty if your repository doesn't use Secrets Manager."
  type        = string
  default     = ""
}

variable "repository_class" {
  description = "REQUIRED. Dotted path 'module.path:ClassName' to your UserRepository implementation (see src/common/repositories/base.py and docs/extending-the-repository.md). There is no default -- this module has no opinion on your database or schema. Your module must be bundled into the Lambda deployment package alongside src/ (see examples/postgres/prepare_for_lambda.sh for the pattern used to vendor an example's own dependencies, as a template for your own)."
  type        = string
}

variable "additional_iam_policy_json" {
  description = "Extra IAM policy JSON (as produced by data.aws_iam_policy_document) attached to ALL THREE Lambda roles, for whatever permissions your UserRepository implementation needs -- e.g. dynamodb:PutItem/GetItem for a DynamoDB-backed repository, or secretsmanager:GetSecretValue if you're not using db_secret_arn's built-in grant. Leave empty if db_secret_arn alone covers what you need."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Networking (existing VPC -- not created here)
# ---------------------------------------------------------------------------
# If your database is reachable without VPC placement (e.g. RDS with a
# public endpoint restricted by security group, or a database product
# with its own network-independent auth like Neon/Supabase pooled
# connections), leave vpc_config as null and the Lambdas run in the
# AWS-managed, non-VPC environment. If your database is in a private
# subnet (the common, recommended case for RDS), set vpc_config so the
# Lambdas can reach it.
variable "vpc_config" {
  description = "VPC configuration for the Lambda functions, matching your database's network placement. Leave null if your database does not require VPC connectivity."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

# ---------------------------------------------------------------------------
# Reconciler schedule
# ---------------------------------------------------------------------------
variable "reconciler_schedule_expression" {
  description = "EventBridge schedule expression for how often the reconciler runs drift detection."
  type        = string
  default     = "rate(15 minutes)"
}

# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
variable "alert_email" {
  description = "Email to subscribe to the module's SNS alert topic. Leave empty to skip; you can subscribe your own endpoints to the alerts_topic_arn output instead (e.g. to route into an existing incident-management SNS topic or Slack integration)."
  type        = string
  default     = ""
}

variable "existing_alerts_topic_arn" {
  description = "ARN of an existing SNS topic to publish alarms to, instead of creating a new one. Use this if you already have an alerting/on-call SNS topic and want this module's alarms to flow into it rather than creating a separate topic."
  type        = string
  default     = ""
}

variable "drift_alarm_threshold" {
  description = "Total drift count that triggers the drift-accumulation alarm."
  type        = number
  default     = 5
}

variable "drift_alarm_evaluation_periods" {
  description = "Number of consecutive reconciler runs the drift count must stay at or above the threshold before alarming."
  type        = number
  default     = 2
}

# ---------------------------------------------------------------------------
# Lambda tuning
# ---------------------------------------------------------------------------
variable "lambda_timeout_seconds" {
  description = "Timeout for the sync Lambdas (post_confirmation/post_authentication). Cognito's own hard limit for these triggers is 5 seconds regardless of this setting."
  type        = number
  default     = 5
}

variable "reconciler_timeout_seconds" {
  description = "Timeout for the reconciler Lambda, which may need longer for large user pools."
  type        = number
  default     = 60
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
