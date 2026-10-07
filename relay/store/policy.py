"""Slow-moving policy state: access, secrets, throttle, identity.

Grants, denials, shared secrets, rate-limit counters, and the known-user
list. Changes here are operator or attacker driven, not per-submission --
that is why it lives apart from ledger.py, whose rows turn over with every
Screenshot. Same sqlite file, shared plumbing in db.py.
"""

import time

import relay.store.db as db

# ---------- access control ----------


def grant(chat_id: int, how: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO access (chat_id, how, granted_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET how=excluded.how",
            (chat_id, how, db.now()),
        )


def claim_if_empty(chat_id: int) -> bool:
    """First claim wins, atomically.

    One statement: the INSERT only fires when the access table is empty, so
    two simultaneous first /starts cannot both be told they won. Stamps
    ("claim", now) itself -- first-claim is the only write shaped this way.
    """
    with db.conn() as c:
        row = c.execute(
            "INSERT INTO access (chat_id, how, granted_at) "
            "SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM access)",
            (chat_id, "claim", db.now()),
        )
        return row.rowcount == 1


def has_access(chat_id: int) -> str | None:
    with db.conn() as c:
        r = c.execute("SELECT how FROM access WHERE chat_id=?", (chat_id,)).fetchone()
    return r["how"] if r else None


def revoke(chat_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM access WHERE chat_id=?", (chat_id,))


def all_access() -> list[dict[str, object]]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM access ORDER BY granted_at")]


def deny(chat_id: int, reason: str = "") -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO denied (chat_id, reason, denied_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET reason=excluded.reason",
            (chat_id, reason, db.now()),
        )


def is_denied(chat_id: int) -> bool:
    with db.conn() as c:
        r = c.execute("SELECT 1 FROM denied WHERE chat_id=?", (chat_id,)).fetchone()
    return r is not None


def undeny(chat_id: int) -> bool:
    with db.conn() as c:
        c.execute("DELETE FROM denied WHERE chat_id=?", (chat_id,))
    return True


def all_denied() -> list[dict[str, object]]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM denied ORDER BY denied_at")]


# ---------- shared secrets ----------


def add_secret(label: str, secret: str) -> None:
    """Store a per-person Shared Secret. Only a hash is kept."""
    import hashlib

    h = hashlib.sha256(("relay-shared-secret:" + secret).encode("utf-8")).hexdigest()
    with db.conn() as c:
        c.execute(
            "INSERT INTO shared_secrets (label, secret_hash, created_at) "
            "VALUES (?,?,?) ON CONFLICT(label) DO UPDATE SET "
            "secret_hash=excluded.secret_hash",
            (label, h, db.now()),
        )


def check_secret(candidate: str) -> str | None:
    """True if candidate matches any stored Shared Secret. Returns the label."""
    import hashlib
    import hmac

    h = hashlib.sha256(("relay-shared-secret:" + candidate).encode("utf-8")).hexdigest()
    with db.conn() as c:
        for r in c.execute("SELECT label, secret_hash FROM shared_secrets"):
            if hmac.compare_digest(h, r["secret_hash"]):
                return r["label"]
    return None


def list_secret_labels() -> list[str]:
    with db.conn() as c:
        return [
            r["label"]
            for r in c.execute("SELECT label FROM shared_secrets ORDER BY label")
        ]


def remove_secret(label: str) -> bool:
    with db.conn() as c:
        c.execute("DELETE FROM shared_secrets WHERE label=?", (label,))
    return True


def init_secrets_from_env(spec: str | None) -> tuple[bool, int]:
    """Load SHARED_SECRETS from the environment if any are configured.

    Returns (loaded_now, total). Does nothing when spec is None, so an operator
    who adds secrets with /addsecret is not clobbered by a restart with an
    unchanged environment. An empty or comment-only spec is likewise a no-op:
    nothing is cleared.
    """
    if spec is None:
        return False, len(list_secret_labels())
    entries = _parse_secret_spec(spec)
    if not entries:
        return False, 0
    with db.conn() as c:
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
    a person by which line to revoke). An empty or comment-only spec is a
    no-op: the stored set is left untouched.
    """
    if spec is None:
        return []
    entries = _parse_secret_spec(spec)
    if not entries:
        return []
    with db.conn() as c:
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
    with db.conn() as c:
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
    with db.conn() as c:
        r = c.execute(
            "SELECT last_try FROM secret_attempts WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return r["last_try"] if r else 0.0


def note_secret_attempt(chat_id: int, ok: bool) -> None:
    with db.conn() as c:
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
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO telegram_users (chat_id, username, first_seen) "
            "VALUES (?,?,?)",
            (chat_id, username, db.now()),
        )


def known_users() -> list[dict[str, object]]:
    with db.conn() as c:
        return [
            dict(r)
            for r in c.execute("SELECT * FROM telegram_users ORDER BY first_seen")
        ]
