#!/usr/bin/env bash
# Provisions a demo Cognito User Pool inside LocalStack and creates a test
# user, so `reconciler/run.py` has something real to reconcile against.
#
# Requires: awslocal (pip install awscli-local) or plain aws CLI pointed
# at --endpoint-url=http://localhost:4566
set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"

echo "Creating Cognito User Pool in LocalStack..."
POOL_ID=$(aws --endpoint-url=$ENDPOINT --region $REGION cognito-idp create-user-pool \
  --pool-name "ccp-demo-pool" \
  --query "UserPool.Id" --output text)

echo "User Pool created: $POOL_ID"
echo "$POOL_ID" > .demo-user-pool-id

echo "Creating a demo user (confirmed, so list-users shows it immediately)..."
aws --endpoint-url=$ENDPOINT --region $REGION cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "alice" \
  --user-attributes Name=email,Value=alice@example.com \
  --message-action SUPPRESS

echo "Done. Export this for later commands:"
echo "  export USER_POOL_ID=$POOL_ID"
