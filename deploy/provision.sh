#!/usr/bin/env bash
# Provision an Oracle Cloud Always Free VM (Ubuntu 24.04) for the Relay.
#
# Sized for VM.Standard.E2.1.Micro: 1GB RAM. That is the only Always Free shape
# guaranteed in most regions, and it is tight -- the site's OCR runs in the
# browser, so the browser exists only while a Screenshot is in flight. See
# docs/adr/0004-browser-lifecycle-on-1gb-host.md.
#
# Run as a user with sudo, on a fresh VM. Idempotent.
set -euo pipefail

APP_DIR=/opt/sparky-relay-2026
APP_USER=relay

echo "==> swap: the safety net for a 1GB host"
# Without swap, a browser launch can OOM-kill the bot. With it, the kernel
# evicts cold pages instead and the run survives. 1GB of swap costs nothing on
# a 1GB box because it is only used under pressure.
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  # Persist across reboot.
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "==> vm.swappiness: prefer reclaiming page cache over swapping"
# The OCR needs its model resident; evicting cold cache is better than
# thrashing the model out to disk.
sysctl -w vm.swappiness=10 >/dev/null
grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

echo "==> firefox"
if ! command -v firefox >/dev/null 2>&1; then
  # The snap build is what Ubuntu ships; the bot reads FIREFOX_BIN from
  # secrets.env and defaults to /usr/bin/firefox, which snap provides.
  if ! snap list firefox >/dev/null 2>&1; then
    snap install firefox
  fi
fi
firefox --version

echo "==> python"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates
python3 --version

echo "==> service user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/"$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> application at $APP_DIR"
mkdir -p "$APP_DIR"
if [ -n "${RELAY_SRC:-}" ]; then
  echo "    copying from $RELAY_SRC"
  rsync -a --exclude '.venv' --exclude '.git' "$RELAY_SRC"/ "$APP_DIR"/
else
  echo "    source dir not set; set RELAY_SRC=/path/to/sparky-relay-2026"
  echo "    then re-run, or copy the files in yourself"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod +x "$APP_DIR"/*.sh 2>/dev/null || true

if [ -f "$APP_DIR/secrets.env" ]; then
  chown "$APP_USER":"$APP_USER" "$APP_DIR/secrets.env"
  chmod 600 "$APP_DIR/secrets.env"
  echo "    secrets.env present, mode 600"
else
  echo "    !! secrets.env missing — create it as $APP_USER before starting"
fi

echo "==> setup"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && ./setup.sh"

echo "==> systemd unit"
install -m 644 "$APP_DIR/deploy/relay.service" /etc/systemd/system/relay.service
systemctl daemon-reload
systemctl enable relay

cat <<'EOF'

Done. Next:

  1. Fill in secrets.env as the relay user:
       sudo -u relay nano /opt/sparky-relay-2026/secrets.env
     Needs TELEGRAM_BOT_TOKEN, SITE_USERNAME, SITE_PASSWORD.

  2. Start it:
       systemctl start relay
       journalctl -u relay -f

  3. Message the bot on Telegram. /status shows your chat id.

  4. Pin it so nobody else can drive the bot:
       ALLOWED_CHAT_ID=<your id>   in secrets.env
       systemctl restart relay

  5. Check it can afford the browser:
       systemctl show relay -p MemoryMax
       journalctl -u relay | grep -i memory
EOF
