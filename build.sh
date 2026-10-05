#!/usr/bin/env bash
# Build the image. Uses podman if present, else docker.
#
#   ./build.sh            build
#   ./build.sh --no-cache build from scratch
#
# --format docker is deliberate: podman drops HEALTHCHECK when building in the
# default OCI format. docker-compose.yml carries an equivalent healthcheck so
# the memory budget is still verified under podman either way.
set -euo pipefail
cd "$(dirname "$0")"

EXTRA=()
if [ "${1:-}" = "--no-cache" ]; then
  EXTRA+=(--no-cache)
fi

if command -v podman >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
else
  echo "neither podman nor docker found" >&2
  exit 1
fi

echo "==> building with $ENGINE"
$ENGINE build --format docker "${EXTRA[@]}" -t sparky-relay-2026:latest .

echo
$ENGINE images sparky-relay-2026 --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
echo
echo "Next:  podman compose up -d relay     (or docker compose up -d relay)"
echo "       podman compose logs -f relay"
