#!/usr/bin/env bash
# Deploy the RichIris working copy from this Windows machine to the Debian box.
#
# Uses tar-over-ssh (no rsync in Git Bash). Copies an explicit include list so
# box-only files (bootstrap.yaml, dependencies/go2rtc/go2rtc, venv/, secrets/)
# are never touched.
#
# Usage (from Git Bash):
#   ./scripts/deploy_box.sh                # code only (fast, day-to-day)
#   ./scripts/deploy_box.sh --with-models  # also push dependencies/models (~530 MB, first deploy)
set -euo pipefail

# Repo root, derived from this script so it survives the working copy moving
# (it used to be hardcoded to /c/01-Self-Hosting/RichIris, which no longer exists).
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="offload"
DEST="/opt/richiris"

INCLUDE=(backend "Camera Light Control" scripts)
if [[ "${1:-}" == "--with-models" ]]; then
    INCLUDE+=(dependencies/models)
fi

echo "Deploying to ${HOST}:${DEST}: ${INCLUDE[*]}"
tar -C "$SRC" \
    --exclude='__pycache__' \
    --exclude='backend/data' \
    --exclude='backend/dist' \
    --exclude='backend/build' \
    --exclude='*.pyc' \
    -cf - "${INCLUDE[@]}" \
  | ssh "$HOST" "mkdir -p ${DEST} && tar -C ${DEST} -xf -"

echo "Done. Restart the service on the box to pick up changes:"
echo "  ssh ${HOST} 'sudo systemctl restart richiris'"
