#!/usr/bin/env bash
# Replaces the whole database with a backup. Everything written since that backup is lost.
#   ./scripts/restore.sh backups/kt-20261116-031500.sql.gz [compose file]
set -euo pipefail
cd "$(dirname "$0")/.."
backup="${1:?Usage: ./scripts/restore.sh <backup file> [compose file]}"
compose_file="${2:-docker-compose.yml}"
[ -f "$backup" ] || { echo "No such file: $backup"; exit 1; }
gunzip -t "$backup" || { echo "$backup is damaged. Nothing changed."; exit 1; }  # check before deleting anything
read -r -p "This replaces all current data with $backup. Type yes to go on: " answer
[ "$answer" = "yes" ] || { echo "Stopped. Nothing changed."; exit 1; }
dc() { docker compose -f "$compose_file" "$@"; }
dc stop api
dc exec -T db psql -U kt -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE kt WITH (FORCE)" -c "CREATE DATABASE kt"
gunzip -c "$backup" | dc exec -T db psql -U kt -d kt -v ON_ERROR_STOP=1 -q -o /dev/null  # errors still show
dc start api  # recreates the kt_app login and its grants
echo "Restored $backup"
