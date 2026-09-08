#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: CONFIRM_RESTORE=codito $0 /absolute/path/to/codito-TIMESTAMP.dump" >&2
  exit 2
fi
if [ "${CONFIRM_RESTORE:-}" != "codito" ]; then
  echo "set CONFIRM_RESTORE=codito after reading docs/operations/backup-restore.md" >&2
  exit 2
fi

backup=$1
case "$backup" in
  /*) ;;
  *) echo "backup path must be absolute" >&2; exit 2 ;;
esac
test -f "$backup"
test -f "$backup.sha256"
(cd "$(dirname "$backup")" && sha256sum -c "$(basename "$backup").sha256")

DEPLOY_ROOT=${DEPLOY_ROOT:-/data/projects/codito}
COMPOSE_FILE="$DEPLOY_ROOT/compose.yaml"
ENV_FILE="$DEPLOY_ROOT/.env"
cd "$DEPLOY_ROOT"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop relay
docker exec -i -u postgres postgres \
  pg_restore --username=postgres --dbname=codito --clean --if-exists --no-owner < "$backup"
redis_db=$(sed -n 's/^CODITO_REDIS_DB=//p' "$ENV_FILE" | tail -n 1)
case "$redis_db" in
  ''|*[!0-9]*) echo "invalid CODITO_REDIS_DB in $ENV_FILE" >&2; exit 1 ;;
esac
docker exec redis sh -lc \
  "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli -n $redis_db FLUSHDB"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile tools run --rm migrate
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d relay

echo "Restore submitted; verify readiness, OAuth metadata, device revocation state, and audit counts."
