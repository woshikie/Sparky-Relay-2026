"""Screenshots awaiting confirmation: the date choice plus Commit.

One module owns what three call sites used to do to a bare global: photo.py
writes the record at intake, callbacks.py reads and mutates it through the
date flow and Commit, commands.py only asks how many are outstanding. The
lookup order lives here too -- exact key first, latest-for-chat fallback --
so the race (two screenshots, one chat, both keyboards retargeting the
newer) is a documented property of latest_for_chat, not an accident spread
across two handlers.

Records expire lazily after PENDING_TTL, the same 600 seconds as the
Credentials Prompt stage: one mental model for unfinished business. Expiry
is checked on read, never by a sweeper; there is no background task in
this process except the nightly backup.

Restart loss is accepted, not handled: the records die with the process --
the live Message in each one (the scratch progress message) could never
persist anyway -- the ledger's overwrite guard survives, and the next tap
says expired.
"""

import datetime
import time
from typing import NotRequired, TypedDict

from telegram import Message


# Pending Screenshot state, keyed by (chat_id, message_id) of the intake
# message. The intake always sets path/steps/reported/scratch/date; the
# date flow adds iso/label/prev later. `at` is stamped by put(), never by
# callers, and drives the lazy expiry.
class Pending(TypedDict):
    path: str
    steps: int
    reported: str
    scratch: Message
    date: datetime.date | None
    at: NotRequired[float]
    iso: NotRequired[str]
    label: NotRequired[str]
    prev: NotRequired[dict[str, object] | None]


PENDING_TTL = 600.0

_pending: dict[tuple[int, int], Pending] = {}


def put(key: tuple[int, int], record: Pending) -> Pending:
    """Store a record, stamped now. Returns it, for alias-mutation callers."""
    record["at"] = time.time()
    _pending[key] = record
    return record


def _expired(record: Pending) -> bool:
    at = record.get("at")
    return at is not None and time.time() - at > PENDING_TTL


def get(key: tuple[int, int]) -> Pending | None:
    """The live record for key, or None if missing or expired.

    Live, deliberately: the date flow mutates date/iso/label/prev in place.
    """
    record = _pending.get(key)
    if record is None:
        return None
    if _expired(record):
        _pending.pop(key, None)
        return None
    return record


def pop(key: tuple[int, int], default: Pending | None = None) -> Pending | None:
    """Drop a record: cancel, commit, or an unreadable second OCR pass."""
    return _pending.pop(key, default)


def latest_for_chat(chat_id: int) -> tuple[tuple[int, int], Pending] | None:
    """Newest live record for a chat, sweeping its expired ones.

    Highest intake message_id wins -- intake ids increase, so newest message
    is newest screenshot. Two unconfirmed screenshots in one chat therefore
    retarget both keyboards at the newer one; the older resurfaces once the
    newer is popped. That is the documented race, kept in one place.
    """
    cands = [(k, r) for k, r in _pending.items() if k[0] == chat_id]
    live = []
    for key, record in cands:
        if _expired(record):
            _pending.pop(key, None)
        else:
            live.append((key, record))
    if not live:
        return None
    return max(live, key=lambda kv: kv[0][1])


def count() -> int:
    """Outstanding confirmations across all chats, for /status."""
    return len(_pending)


def clear() -> None:
    """Drop everything. Tests, and nothing else."""
    _pending.clear()
