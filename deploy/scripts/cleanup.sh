#!/bin/sh
set -eu

DEPLOY_ROOT=${DEPLOY_ROOT:-/data/projects/codito}
cd "$DEPLOY_ROOT"
docker compose --env-file .env -f compose.yaml --profile tools run --rm cleanup
