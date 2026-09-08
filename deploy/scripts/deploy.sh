#!/bin/sh
set -eu

DEPLOY_ROOT=${DEPLOY_ROOT:-/data/projects/codito}
COMPOSE_FILE="$DEPLOY_ROOT/compose.yaml"
ENV_FILE="$DEPLOY_ROOT/.env"

if [ ! -f "$COMPOSE_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "missing $COMPOSE_FILE or $ENV_FILE" >&2
  exit 1
fi

env_mode=$(stat -c '%a' "$ENV_FILE")
if [ "$env_mode" != "600" ]; then
  echo "$ENV_FILE must have mode 0600 (found $env_mode)" >&2
  exit 1
fi

image=$(sed -n 's/^CODITO_RELAY_IMAGE=//p' "$ENV_FILE" | tail -n 1)
case "$image" in
  ""|*:latest)
    echo "CODITO_RELAY_IMAGE must use an immutable version-sha tag, never latest" >&2
    exit 1
    ;;
esac

cd "$DEPLOY_ROOT"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile tools run --rm migrate
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile tools run --rm \
  migrate python /app/relay/manage.py provision_desktop_client
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --remove-orphans relay

attempt=0
until curl --fail --silent --show-error --max-time 3 http://192.168.200.39:8094/health/ready >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 20 ]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail 100 relay
    exit 1
  fi
  sleep 3
done

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
echo "Codito relay is ready on 192.168.200.39:8094"
