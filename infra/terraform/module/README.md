# Cognito Consistency Plugin — Terraform module

Adds identity sync, drift reconciliation, and alerting on top of an
**existing** Cognito User Pool and **existing** database. Does not
create a User Pool, a database, or a VPC — see
[`docs/architecture.md`](../../../docs/architecture.md) for why.

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
  source = "github.com/tgpatel025/cognito-consistency-plugin//infra/terraform/module"

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
# root module -- see the note below on why.
resource "aws_cognito_user_pool" "main" {
  # ... your existing pool config ...

  lambda_config {
    post_confirmation   = module.cognito_consistency.post_confirmation_function_arn
    post_authentication = module.cognito_consistency.post_authentication_function_arn
  }
}
```

### Why `lambda_config` isn't set inside this module

`lambda_config` is an attribute of the `aws_cognito_user_pool` resource
itself; Terraform has no separate "attach a trigger to an existing pool"
resource. Since this module doesn't own your pool resource, you attach
the module's function ARNs to your own pool's `lambda_config`, as shown
above.

**A note on cycles**: the reconciler's IAM policy is scoped to
`cognito_user_pool_arn` (unconditionally, in [`iam.tf`](./iam.tf)), so
the module depends on the pool's ARN while the pool's `lambda_config`
depends on the module's Lambda ARNs. Attaching to an **existing** pool
is fine — its ARN is a known value. Creating a brand-new pool and this
module in one apply is a genuine circular reference, which is why this
repo ships no "create everything from scratch" example. Use
[`infra/localstack`](../localstack) to run the whole system without
pre-existing AWS resources.

## Least-privilege IAM

Each Lambda gets its own role (not a shared one):

| Function | Permissions |
|---|---|
| `post_confirmation` | CloudWatch Logs, plus `secretsmanager:GetSecretValue` on exactly `db_secret_arn` if you set it |
| `post_authentication` | Same as above |
| `reconciler` | Same as above, plus `cognito-idp:ListUsers` scoped to exactly `cognito_user_pool_arn`, plus `cloudwatch:PutMetricData` (AWS doesn't support resource-level scoping for this action) |

All three roles also get `additional_iam_policy_json` if you set it —
put any permissions your repository needs beyond (or instead of) Secrets
Manager there.

See [`iam.tf`](./iam.tf) for the exact policy documents.

## Outputs

See [`outputs.tf`](./outputs.tf) — function ARNs, role ARNs (for
granting additional permissions if you extend the schema), and the
alerts SNS topic ARN (for subscribing additional endpoints).
