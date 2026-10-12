"""Runtime: process singletons, the log helper, and the browser lifecycle.

The browser is the expensive part (~640MB measured on a 1GB host), so it
exists only while a Screenshot is in flight: browser_session() launches on
entry and closes on exit, and refuses outright when memory is short. The
Relay itself is a singleton because the browser is a single scarce
resource, not because of any session: every launch uses a fresh profile
and logs in as its chat, so no session ever survives a Screenshot.
"""

import asyncio
import contextlib
import os
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
# only for as long as a Screenshot is in flight.
_site_lock = asyncio.Lock()
_relay: driver.Relay | None = None


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
) -> AsyncIterator[driver.Relay]:
    """Yield a signed-in Relay, then close the browser.

    Transient by design: the browser exists for the duration of one Screenshot
    and is torn down afterwards, so idle RAM is the bot process alone.

    Sign-in happens inside, after start and before yield: every flow needs an
    authenticated browser, and a forgotten sign_in used to surface one step
    later as a confusing "the site changed" rather than a credential failure.
    Passing chat_id None skips sign-in, for read-only decoration use (the
    leaderboard read) -- never for anything that writes.

    This must be an *async* context manager. With the plain
    `contextlib.contextmanager` on an `async def`, every caller doing
    `async with browser_session()` fails at runtime with
    "'_GeneratorContextManager' object does not support the asynchronous
    context manager protocol" -- which is what happened on the first real
    Screenshot, after every test had passed.
    """
    async with _site_lock:
        r = get_relay(progress)
        # InsufficientMemory propagates as itself: it already carries the
        # numbers, and wrapping it in a second exception type meant callers had
        # to catch both for one condition.
        rep = await asyncio.to_thread(memory.require_memory)
        print(
            "[relay] launching browser (%.0fMB free)" % (rep["available_mb"] or 0),
            flush=True,
        )
        try:
            await asyncio.to_thread(r.start)
            if chat_id is not None:
                await sign_in(chat_id, progress)
            yield r
        finally:
            print("[relay] closing browser", flush=True)
            if progress is not None:
                progress.finish("closing")
            await asyncio.to_thread(r.stop)
            if progress is not None:
                progress.finish()
            mem_after = memory.mem_mb()
            if mem_after:
                print("[relay] %.0fMB available after close" % mem_after, flush=True)


async def site_login(
    ctx: ContextTypes.DEFAULT_TYPE | None, force: bool = False
) -> NoReturn:
    """Sign in using the credentials held for this chat.

    The old ledger "session_valid" shortcut is gone: it was never written to, so
    it always reported stale, and with prompted credentials the browser profile
    is what actually decides whether a re-login is needed.
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
