#!/bin/sh
# Run the Relay. Keeps one browser alive only while a Screenshot is in flight.
#
# POSIX sh, not bash: this runs inside an Alpine container, which has no bash.
set -eu

cd "$(dirname "$0")"

# config.py is the real validation. It checks that the bot token is present and
# that ACCESS_MODE names a valid mode, and refuses to start otherwise. Checking
# for a secrets.env *file* here would break the container, where secrets arrive
# as environment variables instead.
if [ ! -x .venv/bin/python ]; then
  echo "venv missing -- the image should have built it; try ./build.sh" >&2
  exit 1
fi

mkdir -p logs inbox

# exec so SIGTERM from the container runtime reaches Python directly and the
# long-poll shuts down promptly instead of waiting out the grace period.
exec ./.venv/bin/python -u bot.py "$@"
