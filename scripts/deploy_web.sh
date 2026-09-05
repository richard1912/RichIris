#!/usr/bin/env bash
# Build the Flutter web client and publish it to the box's Caddy.
#
# The web build is the SAME app/ source as the Windows and Android clients,
# compiled for the browser. Caddy serves it at richiris.richardferretti.com and
# proxies /api on that same origin to the backend on 192.168.8.12:8700, so the
# client needs no server URL configured and makes no cross-origin request.
#
# Usage:  scripts/deploy_web.sh            (build + deploy)
#         scripts/deploy_web.sh --no-build (deploy the existing build/web)
set -euo pipefail

HOST="${RICHIRIS_BOX:-offload}"
DEST="/opt/stacks/caddy/srv/richiris"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../app" && pwd)"

if [[ "${1:-}" != "--no-build" ]]; then
  echo "==> flutter build web --release"
  (cd "$APP_DIR" && flutter build web --release)
fi

[[ -f "$APP_DIR/build/web/index.html" ]] || {
  echo "no build at $APP_DIR/build/web -- run without --no-build" >&2
  exit 1
}

# Wipe first: Flutter fingerprints its output, so a stale main.dart.js left
# behind from an older build is dead weight that never gets overwritten.
# Piping tar avoids needing rsync on the Windows side.
echo "==> publishing to $HOST:$DEST"
ssh "$HOST" "rm -rf ${DEST:?}/*"
tar -cf - -C "$APP_DIR/build/web" . | ssh "$HOST" "tar -xf - -C $DEST"

# No Caddy reload needed: file_server reads from disk per request, and the
# vhost itself is unchanged.
echo "==> verifying"
ssh "$HOST" "curl -fsS -o /dev/null -w 'index %{http_code}\n' https://richiris.richardferretti.com/ \
          && curl -fsS https://richiris.richardferretti.com/api/health && echo"
