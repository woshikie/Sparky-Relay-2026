"""Config. Nothing here is a default we invented at runtime."""
import os
from contextlib import suppress

from relay.errors import ConfigRefused

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "secrets.env")


def _load():
    """Secrets, from the environment first, then secrets.env.

    The environment wins so a container (which receives secrets as injected env
    vars and has no secrets.env file) works without a second code path. A
    secrets.env file is the bare-metal convenience.
    """
    env = {}
    # RELAY_SKIP_SECRETS_FILE=1 makes the environment the only source, which
    # check_access.py relies on: several cases assert behaviour with and
    # without Preset Credentials, and the developer's own secrets.env would
    # otherwise decide the result of the test.
    skip_file = os.environ.get("RELAY_SKIP_SECRETS_FILE") == "1"
    if not skip_file and os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    # Compose's `${VAR:?}` and `${VAR:-}` distinguish "unset" from "set to the
    # empty string", and an empty env var is how a container expresses "not
    # configured". So an empty value is ignored rather than allowed to blank a
    # value that came from secrets.env.
    for k, v in os.environ.items():
        if v != "" and (k in env or k in KNOWN_KEYS):
            env[k] = v
    return env


# Keys we will accept from the environment, so an unrelated variable in the
# shell cannot silently become configuration.
KNOWN_KEYS = {
    "TELEGRAM_BOT_TOKEN", "SITE_USERNAME", "SITE_PASSWORD", "SITE_BASE",
    "ACCESS_MODE", "DENY_CHAT_IDS", "SHARED_SECRETS",
    "HEADLESS", "FIREFOX_BIN", "BACKUP_TIME",
    "KNOWN_CUTOFF", "RELAY_STATE_DIR", "LEDGER_DB", "INBOX", "LOGS",
}

ENV = _load()

# Access Modes. Whichever is set decides who may drive the Relay.
#   whitelist_claim : first chat to /start claims the Relay. Everyone else
#                     needs to be granted. Safest.
#   blacklist       : open to any chat not denied. PUBLIC write path.
#   shared_secret   : must present a per-person Shared Secret at /start.
ACCESS_MODES = ("whitelist_claim", "blacklist", "shared_secret")
_ACCESS_MODE = (ENV.get("ACCESS_MODE") or "").strip().lower()


def _chat_ids(raw):
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            with suppress(ValueError):
                out.append(int(part))
    return out


# Refuse to start rather than guess. An unset ACCESS_MODE is the difference
# between "only I can use this" and "anyone on the internet can write to my
# leaderboard account", and that is not a default worth assuming.
if not _ACCESS_MODE:
    raise ConfigRefused(
        "\n"
        "ACCESS_MODE is not set. Refusing to start.\n"
        "\n"
        "  It decides who may drive the Relay, and guessing is not safe:\n"
        "    whitelist_claim  first chat to /start claims it (safest)\n"
        "    blacklist        open to anyone not denied  (PUBLIC)\n"
        "    shared_secret    must present a per-person secret at /start\n"
        "\n"
        "  Set it in secrets.env, e.g.   ACCESS_MODE=whitelist_claim\n")

if _ACCESS_MODE not in ACCESS_MODES:
    raise ConfigRefused(
        "\n"
        "ACCESS_MODE=%r is not a valid Access Mode.\n"
        "  expected one of: %s\n" % (_ACCESS_MODE, ", ".join(ACCESS_MODES)))

ACCESS_MODE = _ACCESS_MODE
# Chats refused up front, in blacklist mode.
DENY_CHAT_IDS = _chat_ids(ENV.get("DENY_CHAT_IDS", ""))
# Per-person Shared Secrets, "label:secret" per line. Only hashes are stored.
SHARED_SECRETS = ENV.get("SHARED_SECRETS", "") or None

ENV = _load()

TELEGRAM_BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
SITE_USERNAME = ENV.get("SITE_USERNAME", "")
SITE_PASSWORD = ENV.get("SITE_PASSWORD", "")
SITE_BASE = ENV.get("SITE_BASE", "https://example.invalid").rstrip("/")

# Asia/Singapore is the competition's frame; the site itself hardcodes +08:00.
SGT_OFFSET_HOURS = 8

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


def has_preset_credentials():
    """True when Preset Credentials are configured.

    They are an optimisation, never a requirement: the Credentials Prompt
    works with none of them. `require()` deliberately does not demand them.
    """
    return bool(SITE_USERNAME and SITE_PASSWORD)


def require():
    """Fail loudly and specifically if a secret is missing.

    Only the bot token is mandatory. The Site credentials are optional because
    the user supplies them at runtime; ACCESS_MODE has already been validated
    at import time.
    """
    missing = [k for k, v in (
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
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
        "             which loads secrets.env into the environment for you\n"
        "\n"
        "  Note: SITE_USERNAME and SITE_PASSWORD are optional. Leave them out\n"
        "  and the bot will ask you for them at runtime instead."
        % (", ".join(missing), ENV_PATH))
