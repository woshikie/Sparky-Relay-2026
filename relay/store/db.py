"""Shared sqlite plumbing: path, schema, connections, clock.

One file so the ledger (hot rows) and the policy store (grants, secrets,
throttle state) open the same database the same way. Neither imports the
other; both import here.
"""

import os
import sqlite3
import time

from relay import config

DB = config.LEDGER_DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  activity_date TEXT PRIMARY KEY,
  steps         INTEGER NOT NULL,
  reported      TEXT,
  site_label    TEXT,
  recorded_at   TEXT NOT NULL,
  telegram_msg  INTEGER
);

-- What the site itself reports, read back through its own dashboard. Kept
-- apart from `submissions`, which means "the bot wrote this": a day the user
-- entered by hand is real and has to count for the overwrite guard, but it is
-- not something the bot did, and /log must not claim otherwise.
CREATE TABLE IF NOT EXISTS site_days (
  activity_date TEXT PRIMARY KEY,
  steps         INTEGER NOT NULL,
  reported      TEXT,
  synced_at     TEXT NOT NULL
);

-- One set of Site credentials per chat. Per-chat because Access Mode may admit
-- more than one person, and a Submission always belongs to whoever's Site
-- account the credentials belong to -- never the owner's, by accident.
CREATE TABLE IF NOT EXISTS credentials (
  chat_id      INTEGER PRIMARY KEY,
  username     TEXT NOT NULL,
  password_enc TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  preset       INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL
);

-- Chats that passed a Shared Secret, or claimed the Relay in claim mode.
CREATE TABLE IF NOT EXISTS access (
  chat_id    INTEGER PRIMARY KEY,
  how        TEXT NOT NULL,
  granted_at TEXT NOT NULL
);

-- Chats explicitly refused, in blacklist mode.
CREATE TABLE IF NOT EXISTS denied (
  chat_id    INTEGER PRIMARY KEY,
  reason     TEXT,
  denied_at  TEXT NOT NULL
);

-- Per-person Shared Secrets. One row per person so revoking one does not
-- invalidate the others.
CREATE TABLE IF NOT EXISTS shared_secrets (
  label      TEXT PRIMARY KEY,
  secret_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Rate limiting on failed Shared Secret attempts, so the passphrase is not
-- guessable at Telegram's message rate. A lockout is deliberately absent: it
-- would let anyone keep the owner out of their own bot.
CREATE TABLE IF NOT EXISTS secret_attempts (
  chat_id    INTEGER PRIMARY KEY,
  fails      INTEGER NOT NULL DEFAULT 0,
  last_try   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telegram_users (
  chat_id    INTEGER PRIMARY KEY,
  username   TEXT,
  first_seen TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    return c


def init() -> str:
    d = os.path.dirname(DB)
    if d:
        os.makedirs(d, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    return DB
