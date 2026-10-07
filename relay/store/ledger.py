"""Hot rows: Submissions, site days, and per-chat credentials.

Everything here turns over with use -- every Screenshot writes, every sync
refreshes, every login stores. Slow-moving policy (grants, secrets, throttle
counters, identity) lives in policy.py; shared sqlite plumbing in db.py.
Same file on disk, three modules by volatility.
"""

import datetime
from collections.abc import Sequence

import relay.store.db as db
import relay.store.vault as vault

# ---------- submissions ----------


def last_submission(activity_date: str) -> dict[str, object] | None:
    with db.conn() as c:
        r = c.execute(
            "SELECT * FROM submissions WHERE activity_date=?", (activity_date,)
        ).fetchone()
    return dict(r) if r else None


def all_submissions() -> list[dict[str, object]]:
    with db.conn() as c:
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
    stamp = db.now()
    with db.conn() as c:
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
    with db.conn() as c:
        r = c.execute(
            "SELECT * FROM site_days WHERE activity_date=?", (activity_date,)
        ).fetchone()
    return dict(r) if r else None


def all_site_days() -> list[dict[str, object]]:
    with db.conn() as c:
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
    with db.conn() as c:
        c.execute(
            """INSERT INTO submissions
               (activity_date, steps, reported, site_label, recorded_at, telegram_msg)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(activity_date) DO UPDATE SET
                 steps=excluded.steps, reported=excluded.reported,
                 site_label=excluded.site_label, recorded_at=excluded.recorded_at,
                 telegram_msg=excluded.telegram_msg""",
            (activity_date, steps, reported, site_label, db.now(), msg_id),
        )


# ---------- credentials ----------


def save_credentials(
    chat_id: int, username: str, password: str, token: str, preset: bool = False
) -> None:
    with db.conn() as c:
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
                db.now(),
            ),
        )


def load_credentials(chat_id: int, token: str) -> tuple[str, str] | None:
    """Return (username, password) for a chat, or None.

    Raises vault.DecryptionFailed if the stored entry cannot be opened — which
    means the bot token was rotated, and the old credentials are unrecoverable
    by design. The caller must re-prompt rather than fall back to anything.
    """
    with db.conn() as c:
        r = c.execute(
            "SELECT * FROM credentials WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not r:
        return None
    return (r["username"], vault.open_sealed(token, r["password_enc"]))


def credentials_stored(chat_id: int) -> dict[str, object] | None:
    with db.conn() as c:
        r = c.execute(
            "SELECT username, preset, fingerprint, updated_at FROM credentials "
            "WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    return dict(r) if r else None


def forget_credentials(chat_id: int) -> bool:
    with db.conn() as c:
        c.execute("DELETE FROM credentials WHERE chat_id=?", (chat_id,))
    return True


def forget_all_credentials() -> bool:
    with db.conn() as c:
        c.execute("DELETE FROM credentials")
    return True


def note_preset_choice(chat_id: int) -> None:
    """Remember that this chat chose the Preset Credentials."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO preset_choice (chat_id, chosen_at) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            " chosen_at=excluded.chosen_at",
            (chat_id, db.now()),
        )


def has_preset_choice(chat_id: int) -> bool:
    with db.conn() as c:
        r = c.execute(
            "SELECT 1 FROM preset_choice WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return r is not None


def forget_preset_choice(chat_id: int) -> None:
    """Drop the preset choice: logout means ask again next time."""
    with db.conn() as c:
        c.execute("DELETE FROM preset_choice WHERE chat_id=?", (chat_id,))
