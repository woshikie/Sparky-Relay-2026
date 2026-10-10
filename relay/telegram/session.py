"""Runtime: process singletons, the log helper, and the browser lifecycle.

The browser is the expensive part (~640MB measured on a 1GB host), so it is
transient by default: browser_session() launches on entry and closes on exit,
and refuses outright when memory is short. The photo read phase may instead
hold it briefly (HOLD_BROWSER_SECS) for the coming Confirm, so the user does
not pay a ~20s relaunch after tapping. The Relay itself is a singleton
because the browser is a single scarce resource, not because of any session:
every launch uses a fresh profile and logs in as its chat, so no session
ever crosses a chat boundary or survives a close; a held browser is
same-chat-only and re-validates on reuse (re-logs-in only if its session
died; see browser_session).
"""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import NoReturn

from telegram.ext import ContextTypes

import relay.progress as progress_mod
import relay.telegram.prompts as prompts_mod
from relay import config, memory
from relay.site import driver

DBG = bool(os.environ.get("RELAY_DEBUG"))


def log(ctx: ContextTypes.DEFAULT_TYPE | None, msg: str) -> None:
    if DBG and ctx is not None:
        queue = ctx.job_queue
        if queue is not None:
            with suppress(Exception):
                queue.run_once(_noop_job, 0)  # no-op; keeps job_queue referenced
    print("[relay] %s" % msg, flush=True)


async def _noop_job(_context: object) -> None:
    """Debug no-op for log(): scheduled, never awaited for its result."""


# The site's OCR runs in the page, so the browser is the expensive part: ~640MB
# measured. On a 1GB host it must not be held open. One Relay at a time, and
# by default only for as long as a Screenshot is in flight.
_site_lock = asyncio.Lock()
_relay: driver.Relay | None = None

# A held browser, if any: which chat it belongs to and when the hold lapses
# (monotonic clock). At most one browser ever -- the hold only delays the
# close, never duplicates it. Expiry is lazy: observed on the next acquire,
# never by a timer task (no background tasks except the nightly backup).
_held_chat: int | None = None
_held_until: float = 0.0


def get_relay(
    progress: progress_mod.Progress | None = None,
) -> driver.Relay:
    """The single Relay, with a progress reporter if one is supplied.

    The reporter is attached per Screenshot rather than at construction: the
    Relay outlives any one upload, so holding a reference to a Telegram message
    here would keep editing a message from a submission three days ago.
    """
    global _relay
    if _relay is None:
        _relay = driver.Relay(config.SITE_BASE, headless=config.HEADLESS)
    _relay.progress = progress
    return _relay


@contextlib.asynccontextmanager
async def browser_session(
    chat_id: int | None = None,
    progress: progress_mod.Progress | None = None,
    hold: bool = False,
) -> AsyncIterator[driver.Relay]:
    """Yield a signed-in Relay, then close the browser (usually).

    Transient by design: the browser exists for the duration of one Screenshot
    and is torn down afterwards, so idle RAM is the bot process alone. The one
    exception is the read phase of a confirm flow (hold=True, photo intake
    only): on a clean exit the browser stays open up to HOLD_BROWSER_SECS for
    that chat, so Confirm reuses it instead of paying a relaunch, then closes
    it on its own (hold=False) exit.

    Sign-in happens inside, after start and before yield: every flow needs an
    authenticated browser, and a forgotten sign_in used to surface one step
    later as a confusing "the site changed" rather than a credential failure.
    Passing chat_id None skips sign-in, for read-only decoration use (the
    leaderboard read) -- never for anything that writes. A None acquire with
    a live hold borrows that browser without disturbing the hold: no sign-in,
    no deadline change, no close, since standings are global and need no
    identity.

    This must be an *async* context manager. With the plain
    `contextlib.contextmanager` on an `async def`, every caller doing
    `async with browser_session()` fails at runtime with
    "'_GeneratorContextManager' object does not support the asynchronous
    context manager protocol" -- which is what happened on the first real
    Screenshot, after every test had passed.
    """
    global _held_chat, _held_until
    async with _site_lock:
        secs = config.HOLD_BROWSER_SECS
        hold = hold and chat_id is not None and secs > 0
        r = get_relay(progress)
        live = _held_chat is not None and secs > 0 and time.monotonic() < _held_until
        if chat_id is None and live:
            try:
                yield r
            finally:
                if progress is not None:
                    progress.finish()
            return
        reused = chat_id is not None and live and _held_chat == chat_id
        if reused:
            print("[relay] reusing held browser", flush=True)
        else:
            if _held_chat is not None:
                # A foreign or expired hold: evict before launching, since
                # there is only ever one browser.
                print("[relay] closing held browser", flush=True)
                await asyncio.to_thread(r.stop)
                _held_chat = None
                _held_until = 0.0
            # InsufficientMemory propagates as itself: it already carries the
            # numbers, and wrapping it in a second exception type meant
            # callers had to catch both for one condition. It raises before
            # the try below, so a refusal never stops a browser that never
            # started.
            rep = await asyncio.to_thread(memory.require_memory)
            print(
                "[relay] launching browser (%.0fMB free)" % (rep["available_mb"] or 0),
                flush=True,
            )
        completed = False
        try:
            if reused:
                assert chat_id is not None
                # The hold is keyed by chat_id and foreign holds are
                # evicted on acquire, so a live session here is provably
                # this chat's: skip the form login, which would GET
                # /auth on a live session, bounce to /home with no form,
                # and fail as SiteChanged. A dead session mid-hold reads
                # as not authed and falls through to sign_in as before.
                try:
                    still_authed = r._authed()
                except Exception:
                    still_authed = False
                if still_authed:
                    print(
                        "[relay] reusing held browser (still signed in)",
                        flush=True,
                    )
                else:
                    await sign_in(chat_id, progress)
            else:
                await asyncio.to_thread(r.start)
                if chat_id is not None:
                    await sign_in(chat_id, progress)
            yield r
            completed = True
        finally:
            if completed and hold:
                _held_chat = chat_id
                _held_until = time.monotonic() + secs
                print(
                    "[relay] holding browser open for %ds" % secs,
                    flush=True,
                )
                if progress is not None:
                    progress.finish()
            else:
                print("[relay] closing browser", flush=True)
                if progress is not None:
                    progress.finish("closing")
                await asyncio.to_thread(r.stop)
                if progress is not None:
                    progress.finish()
                mem_after = memory.mem_mb()
                if mem_after:
                    print(
                        "[relay] %.0fMB available after close" % mem_after,
                        flush=True,
                    )
                _held_chat = None
                _held_until = 0.0


def refresh_hold(chat_id: int) -> None:
    """Push a held browser's deadline out after date activity for that chat.

    A date tap means the confirmation is still alive, so the hold must not
    lapse mid-decision. No-op when there is no hold for this chat or holding
    is off. Plain (not async): no awaits, so it is atomic on the event loop
    and needs no lock. May revive an expired-but-unobserved hold (expiry is
    lazy, seen only on the next acquire): benign, since the browser is still
    resident and the user is still deciding.
    """
    global _held_until
    if config.HOLD_BROWSER_SECS <= 0 or _held_chat != chat_id:
        return
    _held_until = time.monotonic() + config.HOLD_BROWSER_SECS


def hold_deadline(chat_id: int) -> float | None:
    """The deadline of this chat's hold, or None when it holds nothing.

    A synchronous snapshot for callers that must close only what they armed
    (see close_held's expected_until). The snapshot itself is exact, but a
    rearm can land while close_held waits on the site lock -- safety comes
    from comparing the token inside close_held under the lock, not from
    call adjacency.
    """
    if _held_chat != chat_id:
        return None
    return _held_until


async def close_held(chat_id: int, expected_until: float | None = None) -> None:
    """Close the browser held for this chat now, e.g. on explicit cancel.

    No-op when the hold belongs to nobody or to another chat. When
    expected_until is given, no-op unless the deadline still matches: a
    concurrent read that rearmed (or a date tap that refreshed) moved it,
    so this close is stale and must not destroy the new hold. The deadline
    doubles as the generation token -- no counter to keep in sync.
    """
    global _held_chat, _held_until
    async with _site_lock:
        if _held_chat != chat_id:
            return
        if expected_until is not None and _held_until != expected_until:
            return
        print("[relay] closing browser", flush=True)
        await asyncio.to_thread(get_relay().stop)
        _held_chat = None
        _held_until = 0.0


async def site_login(
    ctx: ContextTypes.DEFAULT_TYPE | None, force: bool = False
) -> NoReturn:
    """Sign in using the credentials held for this chat.

    The old ledger "session_valid" shortcut is gone: it was never written to, so
    it always reported stale. Fresh launches log in unconditionally, so no
    cached session ever decides anything; a reused hold skips login only
    while its own session lives (see browser_session).
    """
    raise RuntimeError("site_login(ctx) is superseded by browser_session()")


async def sign_in(
    chat_id: int, progress: progress_mod.Progress | None = None
) -> driver.Relay:
    """Sign the Relay in for this chat, or raise something we can explain.

    Called by browser_session() after start, so the yielded Relay is already
    authenticated; the caller owns the browser lifetime and never signs in
    separately. Kept public because the lifecycle tests pin it directly:
    wrong credentials, rotated token, and never starting the browser itself.

    `progress` is threaded through rather than read off the Relay so a caller
    cannot silently get a reporter that belongs to an earlier Screenshot.
    """
    r = get_relay(progress)
    username, password = prompts_mod.site_credentials(chat_id)
    await asyncio.to_thread(r.login, username, password)
    return r
