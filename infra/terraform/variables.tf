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
