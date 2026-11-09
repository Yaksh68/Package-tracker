#!/usr/bin/env bash
# Nightly backup (phase 5):
#   - the database, as a compressed pg_dump;
#   - the uploaded zips (upload repos rescan from them).
# Kept for KEEP_DAYS days in BACKUP_DIR on the VM, and copied off it with
# rclone when BACKUP_REMOTE is set (e.g. "oci:packages-tracker-backups").
# A backup only on the VM is lost with the VM.
#
# Run it from cron as the user who runs docker (see docs/DEPLOY-ORACLE.md):
#   15 3 * * *  BACKUP_REMOTE=oci:packages-tracker-backups ~/Package-tracker/deploy/backup.sh >> ~/backups/backup.log 2>&1
#
# The backups hold the users' encrypted keys and tokens; they can only be
# read with APP_SECRET_KEY, which is kept elsewhere (your password manager).
set -euo pipefail
cd "$(dirname "$0")/.."

DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
compose=(docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud)
stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$DIR"

db="$DIR/db-$stamp.sql.gz"
"${compose[@]}" exec -T postgres \
  pg_dump -U packages -d packages_tracker --no-owner --clean --if-exists \
  | gzip -9 > "$db.part"
mv "$db.part" "$db"

uploads="$DIR/uploads-$stamp.tar.gz"
"${compose[@]}" exec -T app sh -c 'cd /data && if [ -d uploads ]; then tar -czf - uploads; fi' > "$uploads.part"
if [ -s "$uploads.part" ]; then mv "$uploads.part" "$uploads"; else rm -f "$uploads.part"; uploads=""; fi

find "$DIR" -maxdepth 1 \( -name 'db-*.sql.gz' -o -name 'uploads-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete

if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "$db" "$BACKUP_REMOTE/" --no-traverse
  [ -n "$uploads" ] && rclone copy "$uploads" "$BACKUP_REMOTE/" --no-traverse
  # the remote keeps what the VM keeps
  rclone delete "$BACKUP_REMOTE/" --min-age "${KEEP_DAYS}d"
fi

echo "$(date -u +%FT%TZ) backup ok: $(basename "$db") ($(du -h "$db" | cut -f1))${uploads:+, $(basename "$uploads")}"
