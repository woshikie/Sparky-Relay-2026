"""Config from secrets.env. Nothing here is a default we invented at runtime."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "secrets.env")


def _load():
    """Secrets, from the environment first, then secrets.env.

    The environment wins so a container (which receives secrets as injected env
    vars and has no secrets.env file) works without a second code path. A
    secrets.env file is the bare-metal convenience.
    """
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if v != "" and (k in env or k in KNOWN_KEYS):
            env[k] = v
    return env


# Keys we will accept from the environment, so an unrelated variable in the
# shell cannot silently become configuration.
KNOWN_KEYS = {
    "TELEGRAM_BOT_TOKEN", "SITE_USERNAME", "SITE_PASSWORD", "SITE_BASE",
    "ALLOWED_CHAT_ID", "HEADLESS", "FIREFOX_BIN", "BACKUP_TIME",
    "KNOWN_CUTOFF", "RELAY_STATE_DIR", "LEDGER_DB", "INBOX", "LOGS",
}

ENV = _load()

TELEGRAM_BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
SITE_USERNAME = ENV.get("SITE_USERNAME", "")
SITE_PASSWORD = ENV.get("SITE_PASSWORD", "")
SITE_BASE = ENV.get("SITE_BASE", "https://example.invalid").rstrip("/")

# Asia/Singapore is the competition's frame; the site itself hardcodes +08:00.
SGT_OFFSET_HOURS = 8

# Only this chat id may drive the Relay. Discovered on first message unless
# pinned here, so pin it once you know it.
ALLOWED_CHAT_ID = ENV.get("ALLOWED_CHAT_ID", "").strip()

HEADLESS = ENV.get("HEADLESS", "1") != "0"

# Where Firefox lives. Overridable because Oracle's image and a laptop
# package it differently.
FIREFOX_BIN = ENV.get("FIREFOX_BIN", "/usr/bin/firefox")

# State directory: ledger, browser profile, downloaded Screenshots, logs.
# In a container this is the mounted volume, not the image.
STATE_DIR = ENV.get("RELAY_STATE_DIR", HERE)
INBOX = os.path.join(STATE_DIR, "inbox")
LEDGER_DB = os.path.join(STATE_DIR, "ledger.sqlite3")
LOGS = os.path.join(STATE_DIR, "logs")

# Refuse to Commit a number outside the plausible band the site itself uses.
MIN_STEPS, MAX_STEPS = 100, 200_000

# Cutoff observed in live global_settings. Re-read at runtime; this is only a
# local courtesy warning, never a substitute for the site's own check.
KNOWN_CUTOFF = ENV.get("KNOWN_CUTOFF", "2026-11-02T15:59:00+00:00")

BACKUP_TIME = ENV.get("BACKUP_TIME", "03:17")


def require():
    """Fail loudly and specifically if a secret is missing.

    Says *where* it looked, because the two ways of configuring this differ: a
    secrets.env file on a host, injected environment variables in a container.
    """
    missing = [k for k, v in (
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("SITE_USERNAME", SITE_USERNAME),
        ("SITE_PASSWORD", SITE_PASSWORD),
    ) if not v]
    if not missing:
        return True
    raise SystemExit(
        "missing configuration: %s\n"
        "\n"
        "  looked in the environment, and in %s\n"
        "\n"
        "  host:      copy secrets.env.example to secrets.env and fill it in\n"
        "  container: export the variables before `compose up`, or use ./run.sh,\n"
        "             which loads secrets.env into the environment for you"
        % (", ".join(missing), ENV_PATH))
