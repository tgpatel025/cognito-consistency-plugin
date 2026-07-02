# Cognito Consistency Platform — Terraform module

Adds identity sync, drift reconciliation, and alerting on top of an
**existing** Cognito User Pool and **existing** database. This module
does not create a User Pool, a database, or a VPC — see
[`docs/architecture.md`](../../../docs/architecture.md) in the repo root
for why.

## What this creates

- `post_confirmation` / `post_authentication` Lambda functions (the sync
  logic in `src/lambdas/`)
- `reconciler` Lambda function (drift detection, `src/reconciler/`), on
  an EventBridge schedule
- Per-function IAM roles, each scoped to exactly what that function does
  (see [`iam.tf`](./iam.tf))
- CloudWatch alarms + an SNS topic for critical failures and drift
  accumulation (see [`alerting.tf`](./alerting.tf))

## What you provide

| Input | What it is |
|---|---|
| `cognito_user_pool_arn` / `cognito_user_pool_id` | Your existing User Pool |
| `repository_class` | **Required.** Dotted path to your `UserRepository` implementation. There is no default — see [`docs/extending-the-repository.md`](../../../docs/extending-the-repository.md) and [`examples/postgres`](../../../examples/postgres) for a ready-to-use starting point |
| `db_secret_arn` (optional) | ARN of a Secrets Manager secret, if your repository reads one — grants `secretsmanager:GetSecretValue` on exactly this ARN. Leave unset if your repository doesn't use Secrets Manager |
| `vpc_config` (optional) | Subnet + security group IDs, if your database requires VPC placement |
| `additional_iam_policy_json` (optional) | Any other IAM permissions your repository needs (e.g. DynamoDB access) |

## Usage

```hcl
module "cognito_consistency" {
  source = "github.com/tgpatel025/cognito-consistency-platform//infra/terraform/module"

  project_name = "myapp"

  # Your existing Cognito User Pool
  cognito_user_pool_arn = aws_cognito_user_pool.main.arn
  cognito_user_pool_id  = aws_cognito_user_pool.main.id

  # Your UserRepository implementation. Using the shipped Postgres
  # example as-is here; see examples/postgres/prepare_for_lambda.sh for
  # how its code and dependencies get bundled into the deployment
  # package (it copies into src/examples_postgres/, hence the path below).
  repository_class = "examples_postgres.repository:PostgresUserRepository"

  # Only needed if your repository reads Secrets Manager (the shipped
  # Postgres example does, via examples/postgres/connection.py).
  # Expected JSON shape for that example: {"host": "...", "port": 5432,
  # "dbname": "...", "username": "...", "password": "..."}
  db_secret_arn = aws_secretsmanager_secret.db_credentials.arn

  # Only needed if your database is in a private subnet (the common case)
  vpc_config = {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda_to_db.id]
  }

  alert_email = "oncall@myapp.com"
}

# Wire the module's Lambdas as this pool's triggers. This lives in YOUR
# root module (not inside this one), because lambda_config is an
# attribute of a resource you already own -- see the note below on why
# this can't be done inside the module itself.
resource "aws_cognito_user_pool" "main" {
  # ... your existing pool config ...

  lambda_config {
    post_confirmation   = module.cognito_consistency.post_confirmation_function_arn
    post_authentication = module.cognito_consistency.post_authentication_function_arn
  }
}
```

### Why `lambda_config` isn't set inside this module

`aws_cognito_user_pool.lambda_config` is an attribute of the pool
resource itself, and Terraform's `aws_cognito_user_pool` resource
requires the **entire pool** to be declared in one resource block —
there's no separate "attach a trigger to an existing pool" resource.
Since this module doesn't own your pool resource, it can't set an
attribute on it. You attach `module.cognito_consistency.post_confirmation_function_arn`
to your own pool's `lambda_config` yourself, as shown above.

**A note on cycles**: if you were creating the pool AND calling this
module in the same root module, be aware that scoping the reconciler's
IAM policy to `cognito_user_pool_arn` (which happens unconditionally in
[`iam.tf`](./iam.tf)) means the module depends on the pool's ARN, while
the pool's `lambda_config` depends on the module's Lambda ARNs — a
genuine circular reference if both are created from scratch together.
This isn't a problem in the normal case this module is designed for
(attaching to a pool that **already exists**, so its ARN is already a
known value, not something being created in the same apply). It only
becomes a problem if you try to provision a brand-new pool and this
module in one shot — which is why this repo does not ship a "create
everything from scratch" Terraform example; use
[`infra/localstack`](../localstack) instead if you want to see the whole
system running without any pre-existing AWS resources.

## Least-privilege IAM

Each Lambda gets its own role (not a shared one):

| Function | Permissions |
|---|---|
| `post_confirmation` | CloudWatch Logs, plus `secretsmanager:GetSecretValue` on exactly `db_secret_arn` if you set it |
| `post_authentication` | Same as above |
| `reconciler` | Same as above, plus `cognito-idp:ListUsers` scoped to exactly `cognito_user_pool_arn`, plus `cloudwatch:PutMetricData` (AWS doesn't support resource-level scoping for this action) |

All three roles also get `additional_iam_policy_json` attached if you set it — this is where any permissions your repository needs beyond (or instead of) Secrets Manager go.

See [`iam.tf`](./iam.tf) for the exact policy documents.

## Outputs

See [`outputs.tf`](./outputs.tf) — function ARNs, role ARNs (for
granting additional permissions if you extend the schema), and the
alerts SNS topic ARN (for subscribing additional endpoints).
