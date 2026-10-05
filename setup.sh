#!/usr/bin/env bash
# One-time setup for a BARE-METAL install (Debian/Ubuntu/RHEL, needs bash).
#
# In the container this is unnecessary: the Dockerfile provides the venv, the
# deps, Firefox and geckodriver. Use `make build` there instead.
#
# `make setup` creates the venv itself and then calls this with --driver-only,
# so it only fetches geckodriver.
#
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

DRIVER_ONLY=0
if [ "${1:-}" = "--driver-only" ]; then
  DRIVER_ONLY=1
fi

GECKO_VERSION="${GECKODRIVER_VERSION:-0.36.0}"
case "$(uname -m)" in
  x86_64)          GECKO_ARCH=linux64 ;;
  aarch64|arm64)   GECKO_ARCH=linux-aarch64 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

if [ "$DRIVER_ONLY" = "0" ]; then
  echo "==> python venv"
  if [ ! -d .venv ]; then
    python3 -m venv .venv
  fi
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "    $(./.venv/bin/python -c 'import sys;print("python", sys.version.split()[0])')"
fi

echo "==> geckodriver $GECKO_VERSION ($GECKO_ARCH)"
mkdir -p bin
if [ ! -x bin/geckodriver ]; then
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 \
    "https://github.com/mozilla/geckodriver/releases/download/v${GECKO_VERSION}/geckodriver-v${GECKO_VERSION}-${GECKO_ARCH}.tar.gz" \
    -o "$tmp/g.tar.gz"
  tar -xzf "$tmp/g.tar.gz" -C "$tmp"
  mv "$tmp/geckodriver" bin/geckodriver
  chmod +x bin/geckodriver
  rm -rf "$tmp"
fi
./bin/geckodriver --version | head -1

if [ "$DRIVER_ONLY" = "1" ]; then
  echo "==> driver ready"
  exit 0
fi

echo "==> firefox"
if [ -x /usr/bin/firefox ]; then
  /usr/bin/firefox --version
else
  echo "    NOT FOUND at /usr/bin/firefox"
  echo "    install it, or set FIREFOX_BIN in secrets.env"
  exit 1
fi

if [ ! -f secrets.env ]; then
  cp secrets.env.example secrets.env
  echo "==> created secrets.env — fill it in before running the bot"
else
  echo "==> secrets.env already exists, leaving it alone"
fi

mkdir -p logs inbox

cat <<'EOF'

Setup done. Next:

  1. edit secrets.env — ACCESS_MODE is required, plus the bot token
  2. make check        prove it works
  3. make up           build the image and start it
  4. message the bot on Telegram; /status shows your chat id
  5. pin ALLOWED_CHAT_ID in secrets.env, then make restart
EOF