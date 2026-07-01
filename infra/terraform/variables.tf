variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used for all resource names"
  type        = string
  default     = "cognito-consistency"
}

variable "db_username" {
  description = "Postgres master username"
  type        = string
  default     = "ccp_admin"
}

variable "db_password" {
  description = "Postgres master password. Pass via TF_VAR_db_password or a .tfvars file that is gitignored -- never commit this."
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Email address to receive SNS alerts for critical sync failures and drift accumulation. Leave empty to skip the email subscription (you can still subscribe other protocols, e.g. Slack via a Chatbot/Lambda subscriber, to the SNS topic manually)."
  type        = string
  default     = ""
}

variable "drift_alarm_threshold" {
  description = "Total drift count that triggers the drift-accumulation alarm."
  type        = number
  default     = 5
}

variable "drift_alarm_evaluation_periods" {
  description = "Number of consecutive 15-minute reconciler runs the drift count must stay at or above the threshold before alarming. Higher values reduce noise from transient blips at the cost of slower detection."
  type        = number
  default     = 2
}
