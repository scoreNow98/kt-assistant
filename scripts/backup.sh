#!/usr/bin/env bash
# Saves the database to backups/kt-<date>.sql.gz and keeps the newest 14.
#   ./scripts/backup.sh                          # local (docker-compose.yml)
#   ./scripts/backup.sh docker-compose.prod.yml  # server
# Daily on a server (crontab -e):  15 3 * * * cd /path/to/kt-assistant && ./scripts/backup.sh docker-compose.prod.yml
set -euo pipefail
cd "$(dirname "$0")/.."
compose_file="${1:-docker-compose.yml}"
mkdir -p backups
out="backups/kt-$(date +%Y%m%d-%H%M%S).sql.gz"
# No owners, grants or schema_state in the dump: on the next start the API applies schema.sql again,
# which recreates the kt_app login and its grants.
docker compose -f "$compose_file" exec -T db pg_dump -U kt --no-owner --no-privileges --exclude-table=schema_state kt | gzip > "$out.part"
mv "$out.part" "$out"  # a failed dump never looks like a finished backup
ls -1t backups/kt-*.sql.gz | tail -n +15 | while read -r old; do rm -- "$old"; done
echo "Saved $out"
