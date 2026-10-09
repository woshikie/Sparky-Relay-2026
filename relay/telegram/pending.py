"""Screenshots awaiting confirmation: one active per chat, the rest queued.

One module owns what three call sites used to do to a bare global: photo.py
stages arrivals, callbacks.py resolves them through the date flow and
Commit, commands.py only asks how many are outstanding. The lookup order
lives here too -- exact key first, latest-for-chat fallback.

One chat holds at most one live confirmation. A Screenshot arriving while
one is live is read immediately (the browser work is unchanged) but
confirmed later, in upload order: it waits in a per-chat FIFO carrying no
buttons of its own, so a tap can never land on the wrong Screenshot.
Resolving the active one -- commit, cancel, failure, or expiry -- presents
the next queued record. Sections that mutate both sides run under
lock_for(chat_id); synchronous reads need no lock on a single event loop,
but every decide-and-stage sequence crosses awaits.

Records expire lazily after PENDING_TTL, the same 600 seconds as the
Credentials Prompt stage: one mental model for unfinished business. Expiry
is checked on read, never by a sweeper; there is no background task in
this process except the nightly backup.

Restart loss is accepted, not handled: the records die with the process --
the live Message in each one (the scratch progress message) could never
persist anyway -- the ledger's overwrite guard survives, and the next tap
says expired.
"""

import asyncio
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
_queues: dict[int, list[Pending]] = {}
_locks: dict[int, asyncio.Lock] = {}


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


def lock_for(chat_id: int) -> asyncio.Lock:
    """The mutate-both-sides lock for a chat.

    Staging (photo intake) and resolving (commit, cancel, failure, expiry
    promotion) each touch the active record and the queue with awaits
    between, so both run inside this lock. Synchronous reads (get,
    latest_for_chat, count) need none: no awaits, no interleaving.
    """
    lock = _locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[chat_id] = lock
    return lock


def has_live(chat_id: int) -> bool:
    """Whether this chat holds an unexpired active confirmation."""
    return latest_for_chat(chat_id) is not None


def enqueue(chat_id: int, record: Pending) -> int:
    """Hold a record behind the active one. Returns its 1-based position."""
    queue = _queues.setdefault(chat_id, [])
    queue[:] = [r for r in queue if not _expired(r)]
    record["at"] = time.time()
    queue.append(record)
    return len(queue)


def requeue_front(chat_id: int, record: Pending) -> None:
    """Put a record back at the head of its chat's queue.

    For presentation that failed before staging: the record was already
    popped, and dropping it would lose the Screenshot silently. A record
    without a stamp (fresh intake never passed put/enqueue) gains one
    now; a retried record keeps its original stamp, so a poison head
    still expires instead of blocking its chat forever.
    """
    record.setdefault("at", time.time())
    _queues.setdefault(chat_id, []).insert(0, record)


def take_next(chat_id: int) -> Pending | None:
    """Oldest live queued record, if the chat has no live active one.

    Returns None when an active confirmation exists (confirm it first) or
    when the queue is empty or all expired. Expired queued records are
    dropped on sight.
    """
    if latest_for_chat(chat_id) is not None:
        return None
    queue = _queues.get(chat_id, [])
    while queue:
        record = queue.pop(0)
        if not _expired(record):
            return record
    return None


def latest_for_chat(chat_id: int) -> tuple[tuple[int, int], Pending] | None:
    """Newest live record for a chat, sweeping its expired ones.

    Highest intake message_id wins. Queued Screenshots carry no buttons,
    so taps cannot land on the wrong record; newest-wins stays as the
    defensive rule for whatever holds a live prompt.
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
    """Outstanding confirmations across all chats, queued included."""
    for key, record in list(_pending.items()):
        if _expired(record):
            _pending.pop(key, None)
    for chat_id, queue in _queues.items():
        _queues[chat_id] = [r for r in queue if not _expired(r)]
    return len(_pending) + sum(len(queue) for queue in _queues.values())


def clear() -> None:
    """Drop everything. Tests, and nothing else."""
    _pending.clear()
    _queues.clear()
    _locks.clear()
