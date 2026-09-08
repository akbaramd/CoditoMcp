#!/bin/sh
set -eu

DEPLOY_ROOT=${DEPLOY_ROOT:-/data/projects/codito}
BACKUP_ROOT=${BACKUP_ROOT:-/data/backups/codito}
COMPOSE_FILE="$DEPLOY_ROOT/compose.yaml"
ENV_FILE="$DEPLOY_ROOT/.env"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
daily_dir="$BACKUP_ROOT/daily"
weekly_dir="$BACKUP_ROOT/weekly"
temporary="$daily_dir/.codito-$timestamp.dump.tmp"
destination="$daily_dir/codito-$timestamp.dump"

umask 077
mkdir -p "$daily_dir" "$weekly_dir"
cd "$DEPLOY_ROOT"

docker exec -u postgres postgres \
  pg_dump --username=postgres --dbname=codito --no-owner --format=custom --compress=9 \
  > "$temporary"

test -s "$temporary"
mv "$temporary" "$destination"
(cd "$daily_dir" && sha256sum "$(basename "$destination")" > "$(basename "$destination").sha256")

if [ "$(date -u +%u)" = "7" ]; then
  cp -p "$destination" "$weekly_dir/$(basename "$destination")"
  cp -p "$destination.sha256" "$weekly_dir/$(basename "$destination.sha256")"
fi

prune_backups() {
  directory=$1
  keep=$2
  find "$directory" -maxdepth 1 -type f -name 'codito-*.dump' -printf '%f\n' \
    | sort -r \
    | awk -v keep="$keep" 'NR > keep' \
    | while IFS= read -r expired; do
        rm -f -- "$directory/$expired" "$directory/$expired.sha256"
      done
}

prune_backups "$daily_dir" 7
prune_backups "$weekly_dir" 4

echo "$destination"
