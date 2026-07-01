#!/usr/bin/env bash
# Terraform's archive_file zips up src/ as-is, but psycopg2-binary is not
# part of the standard Lambda Python runtime and must be vendored into
# the package. This script installs dependencies into src/ before
# `terraform apply` runs the archive step.
#
# Run this once before `terraform apply` (or wire it into CI before deploy).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Installing Lambda-compatible dependencies into src/..."
pip install \
  --platform manylinux2014_x86_64 \
  --target=src \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  psycopg2-binary boto3

echo "Done. src/ now contains vendored dependencies for packaging."
echo "Note: these vendored packages are gitignored -- run this script again after a fresh clone, before 'terraform apply'."
