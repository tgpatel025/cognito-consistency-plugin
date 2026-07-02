#!/usr/bin/env bash
# Prepares this example for Lambda deployment via the Terraform module,
# which zips src/ only (see infra/terraform/module/main.tf) -- code
# outside src/, including this examples/ directory, is not bundled
# automatically. This is deliberate: bundling every example into every
# deployment regardless of use would mean shipping psycopg2 (and its
# compiled binary) to people who chose a different repository.
#
# This script:
#   1. Copies this example's repository/connection code into src/ so
#      it's included in the Lambda zip
#   2. Vendors psycopg2-binary + boto3 into src/ (psycopg2 is not part
#      of the standard Lambda Python runtime)
#
# Run this before `terraform apply` if you're using this example as-is
# (REPOSITORY_CLASS=examples.postgres.repository:PostgresUserRepository).
# If you write your own repository, write your own equivalent of this
# script for whatever your repository needs vendored.
set -euo pipefail

cd "$(dirname "$0")/../.."   # repo root

echo "Copying Postgres example into src/examples_postgres/ for Lambda packaging..."
mkdir -p src/examples_postgres
cp examples/postgres/repository.py examples/postgres/connection.py src/examples_postgres/
touch src/examples_postgres/__init__.py

echo "Installing Lambda-compatible dependencies into src/..."
pip install \
  --platform manylinux2014_x86_64 \
  --target=src \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  psycopg2-binary boto3

echo "Done."
echo "Set REPOSITORY_CLASS=examples_postgres.repository:PostgresUserRepository"
echo "(note the module path changed to examples_postgres, matching where this script copied it within src/)"
echo "Note: src/examples_postgres/ and vendored packages are gitignored -- run this script again after a fresh clone, before 'terraform apply'."
