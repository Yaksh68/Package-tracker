#!/usr/bin/env bash
# Update the running service to the latest main: pull, rebuild the image,
# restart what changed, and drop the old image. Run it on the VM:
#   ~/Package-tracker/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud up -d --build
docker image prune -f >/dev/null
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud ps
