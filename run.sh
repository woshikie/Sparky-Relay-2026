#!/usr/bin/env bash
# Run the Relay, loading secrets from secrets.env first.
#
#   ./run.sh up -d      start detached
#   ./run.sh logs -f    follow logs
#   ./run.sh down       stop and remove
#   ./run.sh ps
#
# `make` wraps this for the common cases (make up, make logs, ...).
#
# Why this exists: the compose file takes secrets as environment variables
# because podman-compose 1.6.0 ignores `env_file:`. So they have to be
# exported before compose interpolates the file, and forgetting that gives a
# container that exits immediately with "missing configuration".
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f secrets.env ]; then
  echo "secrets.env not found — copy secrets.env.example and fill it in" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./secrets.env
set +a

if command -v podman >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
else
  echo "neither podman nor docker found" >&2
  exit 1
fi

exec $ENGINE compose "$@"