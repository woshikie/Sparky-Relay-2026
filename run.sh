#!/usr/bin/env bash
# Convenience wrapper: load secrets, then hand over to the compose provider.
#
#   ./run.sh up -d      start detached
#   ./run.sh logs -f    follow logs
#   ./run.sh down       stop and remove
#   ./run.sh ps
#
# Why this exists: the compose file takes secrets as environment variables
# (podman-compose 1.6.0 ignores `env_file:`), so they have to be exported
# before compose interpolates the file. Forgetting that yields a container that
# exits immediately, which is an unhelpful way to discover it.
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

# 2>/dev/null drops podman-compose's "executing external provider" banner,
# which would otherwise land in the middle of the bot's own output.
exec $ENGINE compose "$@" 2> >(grep -v "external compose provider" >&2)
