"""Durable state: Submissions, credentials, access control, Telegram identity.

Three jobs beyond the Submission ledger:

1. Submissions per Activity Date, so the asymmetric Overwrite guard survives a
   restart.
2. A Credential Vault entry per chat, so a supplied Site password is not held in
   memory indefinitely.
3. Access decisions: who may drive the Relay, under whichever Access Mode is
   configured.
"""

import datetime
import os
import sqlite3
import time
from collections.abc import Sequence

import relay.store.vault as vault
from relay import config

HERE = os.path.dirname(os.path.abspath(__file__))
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


# ---------- submissions ----------


def last_submission(activity_date: str) -> dict[str, object] | None:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM submissions WHERE activity_date=?", (activity_date,)
        ).fetchone()
    return dict(r) if r else None


def all_submissions() -> list[dict[str, object]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM submissions ORDER BY activity_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- site days ----------


def record_site_days(rows: Sequence[tuple[datetime.date, int]]) -> int:
    """Store what the site reports. `rows` is [(date, steps), ...].

    Upsert, so re-syncing refreshes rather than duplicates. Returns the number
    of days written.
    """
    stamp = now()
    with conn() as c:
        for day, steps in rows:
            iso = day.isoformat() if isinstance(day, datetime.date) else str(day)
            c.execute(
                "INSERT INTO site_days (activity_date, steps, reported, synced_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(activity_date) DO UPDATE SET"
                "   steps=excluded.steps, reported=excluded.reported,"
                "   synced_at=excluded.synced_at",
                (iso, int(steps), f"{int(steps):,}", stamp),
            )
    return len(rows)


def site_value(activity_date: str) -> dict[str, object] | None:
    """What the site holds for a date, from the last sync. None if never synced."""
    with conn() as c:
        r = c.execute(
            "SELECT * FROM site_days WHERE activity_date=?", (activity_date,)
        ).fetchone()
    return dict(r) if r else None


def all_site_days() -> list[dict[str, object]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM site_days ORDER BY activity_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def current_value(activity_date: str) -> dict[str, object] | None:
    """What the site holds for a date.

    The sync if we have one, else the bot's own record of what it wrote.

    The site is the authority. A day the user entered by hand is invisible to
    the bot until a sync, and the overwrite guard has to know about it or it
    will happily overwrite a day the bot never wrote.
    """
    row = site_value(activity_date)
    if row:
        return row
    return last_submission(activity_date)


def record(
    activity_date: str,
    steps: int,
    reported: str | None = None,
    site_label: str | None = None,
    msg_id: int | None = None,
) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO submissions
               (activity_date, steps, reported, site_label, recorded_at, telegram_msg)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(activity_date) DO UPDATE SET
                 steps=excluded.steps, reported=excluded.reported,
                 site_label=excluded.site_label, recorded_at=excluded.recorded_at,
                 telegram_msg=excluded.telegram_msg""",
            (activity_date, steps, reported, site_label, now(), msg_id),
        )


# ---------- credentials ----------


def save_credentials(
    chat_id: int, username: str, password: str, token: str, preset: bool = False
) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO credentials
               (chat_id, username, password_enc, fingerprint, preset, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 username=excluded.username,
                 password_enc=excluded.password_enc,
                 fingerprint=excluded.fingerprint,
                 preset=excluded.preset,
                 updated_at=excluded.updated_at""",
            (
                chat_id,
                username,
                vault.seal(token, password),
                vault.fingerprint(token, username),
                1 if preset else 0,
                now(),
            ),
        )


def load_credentials(chat_id: int, token: str) -> tuple[str, str] | None:
    """Return (username, password) for a chat, or None.

    Raises vault.DecryptionFailed if the stored entry cannot be opened — which
    means the bot token was rotated, and the old credentials are unrecoverable
    by design. The caller must re-prompt rather than fall back to anything.
    """
    with conn() as c:
        r = c.execute(
            "SELECT * FROM credentials WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not r:
        return None
    return (r["username"], vault.open_sealed(token, r["password_enc"]))


def credentials_stored(chat_id: int) -> dict[str, object] | None:
    with conn() as c:
        r = c.execute(
            "SELECT username, preset, fingerprint, updated_at FROM credentials "
            "WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    return dict(r) if r else None


def forget_credentials(chat_id: int) -> bool:
    with conn() as c:
        c.execute("DELETE FROM credentials WHERE chat_id=?", (chat_id,))
    return True


def forget_all_credentials() -> bool:
    with conn() as c:
        c.execute("DELETE FROM credentials")
    return True


# ---------- access control ----------


def grant(chat_id: int, how: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO access (chat_id, how, granted_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET how=excluded.how",
            (chat_id, how, now()),
        )


def has_access(chat_id: int) -> str | None:
    with conn() as c:
        r = c.execute("SELECT how FROM access WHERE chat_id=?", (chat_id,)).fetchone()
    return r["how"] if r else None


def revoke(chat_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM access WHERE chat_id=?", (chat_id,))


def all_access() -> list[dict[str, object]]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM access ORDER BY granted_at")]


def deny(chat_id: int, reason: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO denied (chat_id, reason, denied_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET reason=excluded.reason",
            (chat_id, reason, now()),
        )


def is_denied(chat_id: int) -> bool:
    with conn() as c:
        r = c.execute("SELECT 1 FROM denied WHERE chat_id=?", (chat_id,)).fetchone()
    return r is not None


def all_denied() -> list[dict[str, object]]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM denied ORDER BY denied_at")]


# ---------- shared secrets ----------


def add_secret(label: str, secret: str) -> None:
    """Store a per-person Shared Secret. Only a hash is kept."""
    import hashlib

    h = hashlib.sha256(("relay-shared-secret:" + secret).encode("utf-8")).hexdigest()
    with conn() as c:
        c.execute(
            "INSERT INTO shared_secrets (label, secret_hash, created_at) "
            "VALUES (?,?,?) ON CONFLICT(label) DO UPDATE SET "
            "secret_hash=excluded.secret_hash",
            (label, h, now()),
        )


def check_secret(candidate: str) -> str | None:
    """True if candidate matches any stored Shared Secret. Returns the label."""
    import hashlib
    import hmac

    h = hashlib.sha256(("relay-shared-secret:" + candidate).encode("utf-8")).hexdigest()
    with conn() as c:
        for r in c.execute("SELECT label, secret_hash FROM shared_secrets"):
            if hmac.compare_digest(h, r["secret_hash"]):
                return r["label"]
    return None


def list_secret_labels() -> list[str]:
    with conn() as c:
        return [
            r["label"]
            for r in c.execute("SELECT label FROM shared_secrets ORDER BY label")
        ]


def remove_secret(label: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM shared_secrets WHERE label=?", (label,))
    return True


def init_secrets_from_env(spec: str | None) -> tuple[bool, int]:
    """Load SHARED_SECRETS from the environment if any are configured.

    Returns (loaded_now, total). Does nothing when spec is None, so an operator
    who adds secrets with /addsecret is not clobbered by a restart with an
    unchanged environment.
    """
    if spec is None:
        return False, len(list_secret_labels())
    entries = _parse_secret_spec(spec)
    if not entries:
        return False, 0
    with conn() as c:
        c.execute("DELETE FROM shared_secrets")
    for label, secret in entries:
        add_secret(label, secret)
    return True, len(entries)


def _parse_secret_spec(spec: str | None) -> list[tuple[str, str]]:
    entries = []
    for line in (spec or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            label, secret = line.split(":", 1)
            label, secret = label.strip(), secret.strip()
        else:
            secret = line
            label = "secret-%s" % line[:8]
        if secret:
            entries.append((label, secret))
    return entries


def sync_secrets_from_env(spec: str | None) -> list[str]:
    """Load SHARED_SECRETS from the environment, replacing the stored set.

    Format: one entry per line, either "label:secret" or just "secret" (the
    label then defaults to the first 8 characters, which is enough to identify
    a person by which line to revoke).
    """
    if spec is None:
        return []
    entries = _parse_secret_spec(spec)
    with conn() as c:
        c.execute("DELETE FROM shared_secrets")
    for label, secret in entries:
        add_secret(label, secret)
    return [label for label, _ in entries]


# ---------- secret attempt rate limiting ----------

# How long a chat must wait after a wrong Shared Secret before trying again.
# One attempt per window: slow to guess, but a wrong guess never locks anyone
# out of the bot.
SECRET_WINDOW = 30.0


def secret_throttled(
    chat_id: int, window: float | None = None, max_fails: int = 1
) -> bool:
    """True if this chat is guessing too fast and should be made to wait."""
    window = SECRET_WINDOW if window is None else window
    with conn() as c:
        r = c.execute(
            "SELECT fails, last_try FROM secret_attempts WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    if not r:
        return False
    if time.time() - r["last_try"] > window:
        return False
    return r["fails"] >= max_fails


def last_secret_attempt(chat_id: int) -> float:
    with conn() as c:
        r = c.execute(
            "SELECT last_try FROM secret_attempts WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return r["last_try"] if r else 0.0


def note_secret_attempt(chat_id: int, ok: bool) -> None:
    with conn() as c:
        if ok:
            c.execute("DELETE FROM secret_attempts WHERE chat_id=?", (chat_id,))
            return
        r = c.execute(
            "SELECT fails FROM secret_attempts WHERE chat_id=?", (chat_id,)
        ).fetchone()
        fails = (r["fails"] if r else 0) + 1
        c.execute(
            "INSERT INTO secret_attempts (chat_id, fails, last_try) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET fails=excluded.fails, "
            "last_try=excluded.last_try",
            (chat_id, fails, time.time()),
        )


# ---------- telegram identity ----------


def remember_user(chat_id: int, username: str | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO telegram_users (chat_id, username, first_seen) "
            "VALUES (?,?,?)",
            (chat_id, username, now()),
        )


def known_users() -> list[dict[str, object]]:
    with conn() as c:
        return [
            dict(r)
            for r in c.execute("SELECT * FROM telegram_users ORDER BY first_seen")
        ]
