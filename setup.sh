#!/usr/bin/env bash
# One-time setup for a BARE-METAL install (Debian/Ubuntu/RHEL, needs bash).
# In the container this is unnecessary: the Dockerfile provides the venv, the
# deps, Firefox and geckodriver. Use ./build.sh there instead.
#
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

GECKO_VERSION="0.36.0"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  GECKO_ARCH="linux64" ;;
  aarch64|arm64) GECKO_ARCH="linux-aarch64" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

echo "==> python venv"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
echo "    $(./.venv/bin/python -c 'import sys;print("python", sys.version.split()[0])')"

echo "==> geckodriver $GECKO_VERSION ($GECKO_ARCH)"
mkdir -p bin
if [ ! -x bin/geckodriver ]; then
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/v${GECKO_VERSION}/geckodriver-v${GECKO_VERSION}-${GECKO_ARCH}.tar.gz" \
    -o "$tmp/g.tar.gz"
  tar -xzf "$tmp/g.tar.gz" -C "$tmp"
  mv "$tmp/geckodriver" bin/geckodriver
  chmod +x bin/geckodriver
  rm -rf "$tmp"
fi
./bin/geckodriver --version | head -1

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

echo
echo "Setup done. Next:"
echo "  1. edit secrets.env  (bot token + site username/password)"
echo "  2. ./bot.sh          (run it)"
echo "  3. message the bot on Telegram; it will print your chat id"
echo "  4. pin ALLOWED_CHAT_ID in secrets.env and restart"
