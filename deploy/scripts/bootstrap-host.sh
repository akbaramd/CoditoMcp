#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: bootstrap-host.sh <immutable-image> <trusted-proxy-ips-or-cidrs>" >&2
  exit 2
fi

image=$1
trusted_proxies=$2
deploy_root=${DEPLOY_ROOT:-/data/projects/codito}
env_file="$deploy_root/.env"

case "$image" in
  ""|*:latest)
    echo "image must use an immutable version-sha tag, never latest" >&2
    exit 2
    ;;
esac

if [ -z "$trusted_proxies" ] || [ "$trusted_proxies" = "*" ]; then
  echo "trusted proxy IPs/CIDRs must be explicit" >&2
  exit 2
fi

if [ -e "$env_file" ]; then
  echo "$env_file already exists; refusing to replace production secrets" >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required to URL-encode the existing Redis credential" >&2
  exit 1
}
docker container inspect postgres >/dev/null 2>&1 || {
  echo "the existing postgres container is unavailable" >&2
  exit 1
}
docker container inspect redis >/dev/null 2>&1 || {
  echo "the existing redis container is unavailable" >&2
  exit 1
}
docker network inspect postgres-network >/dev/null 2>&1 || {
  echo "the external postgres-network is unavailable" >&2
  exit 1
}
docker network inspect redis-network >/dev/null 2>&1 || {
  echo "the external redis-network is unavailable" >&2
  exit 1
}

install -d -m 0750 "$deploy_root"
key_file=$(mktemp)
trap 'rm -f "$key_file"' EXIT HUP INT TERM
openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$key_file"

django_secret=$(openssl rand -hex 48)
postgres_password=$(openssl rand -hex 32)
metrics_token=$(openssl rand -hex 32)
oidc_key_b64=$(openssl base64 -A -in "$key_file")
redis_password=$(
  docker inspect redis --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n 's/^REDIS_PASSWORD=//p' \
    | tail -n 1
)
if [ -z "$redis_password" ]; then
  echo "the existing redis container does not expose REDIS_PASSWORD" >&2
  exit 1
fi
redis_password_encoded=$(
  REDIS_PASSWORD_VALUE="$redis_password" python3 -c \
    'import os, urllib.parse; print(urllib.parse.quote(os.environ["REDIS_PASSWORD_VALUE"], safe=""))'
)
redis_db=${CODITO_REDIS_DB:-1}
case "$redis_db" in
  ''|*[!0-9]*) echo "CODITO_REDIS_DB must be a non-negative integer" >&2; exit 2 ;;
esac
redis_db_size=$(
  docker exec redis sh -lc \
    "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli --raw -n $redis_db DBSIZE"
)
if [ "$redis_db_size" != "0" ]; then
  echo "Redis logical database $redis_db is not empty; choose an unused CODITO_REDIS_DB" >&2
  exit 1
fi

existing_role=$(
  docker exec -u postgres postgres psql -Atqc \
    "SELECT 1 FROM pg_roles WHERE rolname = 'codito'"
)
existing_database=$(
  docker exec -u postgres postgres psql -Atqc \
    "SELECT 1 FROM pg_database WHERE datname = 'codito'"
)
if [ -n "$existing_role" ] || [ -n "$existing_database" ]; then
  echo "a codito role or database already exists; refusing to assume ownership" >&2
  exit 1
fi

docker exec -i -u postgres postgres psql \
  --set=ON_ERROR_STOP=1 --set="codito_password=$postgres_password" postgres <<'SQL'
SELECT format('CREATE ROLE codito LOGIN PASSWORD %L', :'codito_password')
\gexec
CREATE DATABASE codito OWNER codito;
REVOKE ALL ON DATABASE codito FROM PUBLIC;
GRANT ALL ON DATABASE codito TO codito;
SQL

umask 077
{
  printf '%s\n' "CODITO_RELAY_IMAGE=$image"
  printf '%s\n' "BIND_ADDRESS=192.168.200.39"
  printf '%s\n' "PUBLIC_BASE_URL=https://codito.akbaramd.ir"
  printf '%s\n' "DJANGO_ALLOWED_HOSTS=codito.akbaramd.ir"
  printf '%s\n' "CSRF_TRUSTED_ORIGINS=https://codito.akbaramd.ir"
  printf '%s\n' "FORWARDED_ALLOW_IPS=$trusted_proxies"
  printf '%s\n' "DJANGO_SECRET_KEY=$django_secret"
  printf '%s\n' "DATABASE_URL=postgresql://codito:$postgres_password@postgres:5432/codito"
  printf '%s\n' "REDIS_URL=redis://:$redis_password_encoded@redis:6379/$redis_db"
  printf '%s\n' "CODITO_REDIS_DB=$redis_db"
  printf '%s\n' "METRICS_BEARER_TOKEN=$metrics_token"
  printf '%s\n' "OIDC_RSA_PRIVATE_KEY_B64=$oidc_key_b64"
  printf '%s\n' "DESKTOP_OAUTH_CLIENT_ID=codito-windows-agent"
  printf '%s\n' "CHATGPT_CLIENT_METADATA_HOSTS=chatgpt.com,openai.com"
} >"$env_file"
chmod 0600 "$env_file"

echo "Created $env_file with mode 0600; secret values were not printed."
