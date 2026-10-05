"""Durable per-Activity-Date record of what the Relay recorded.

Two jobs: remember the Site Session so we do not re-login constantly, and
remember the last Submission per Activity Date so the overwrite guard survives a
restart.
"""
import os
import sqlite3
import time

import config

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
CREATE TABLE IF NOT EXISTS session (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  token      TEXT,
  refresh    TEXT,
  expires_at INTEGER,
  username   TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS telegram_users (
  chat_id   INTEGER PRIMARY KEY,
  username  TEXT,
  first_seen TEXT
);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
    return DB


# ---------- submissions ----------

def last_submission(activity_date):
    with conn() as c:
        r = c.execute(
            "SELECT * FROM submissions WHERE activity_date=?", (activity_date,)
        ).fetchone()
    return dict(r) if r else None


def all_submissions():
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM submissions ORDER BY activity_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def record(activity_date, steps, reported=None, site_label=None, msg_id=None):
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


# ---------- session ----------

def save_session(token, refresh, expires_at, username):
    with conn() as c:
        c.execute(
            """INSERT INTO session (id, token, refresh, expires_at, username, updated_at)
               VALUES (1,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 token=excluded.token, refresh=excluded.refresh,
                 expires_at=excluded.expires_at, username=excluded.username,
                 updated_at=excluded.updated_at""",
            (token, refresh, expires_at, username, now()),
        )


def load_session():
    with conn() as c:
        r = c.execute("SELECT * FROM session WHERE id=1").fetchone()
    return dict(r) if r else None


def session_valid(skew=300):
    s = load_session()
    if not s or not s.get("token") or not s.get("expires_at"):
        return False
    return (s["expires_at"] - skew) > time.time()


def clear_session():
    with conn() as c:
        c.execute("UPDATE session SET token=NULL, refresh=NULL, expires_at=0 "
                  "WHERE id=1")


# ---------- telegram identity ----------

def remember_user(chat_id, username=None):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO telegram_users (chat_id, username, first_seen) "
            "VALUES (?,?,?)",
            (chat_id, username, now()),
        )


def known_users():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM telegram_users ORDER BY first_seen")]
