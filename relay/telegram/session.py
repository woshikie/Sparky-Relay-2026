"""Runtime: process singletons, the log helper, and the browser lifecycle.

The browser is the expensive part (~640MB measured on a 1GB host), so it
exists only while a Screenshot is in flight: browser_session() launches on
entry and closes on exit, and refuses outright when memory is short. The
Relay itself is a singleton because the profile -- and therefore the Site
Session -- belongs to the process, not to any one Screenshot.
"""
import asyncio
import contextlib
import os

from relay import config, memory
from relay.site import driver
import relay.telegram.prompts as prompts_mod


DBG = bool(os.environ.get("RELAY_DEBUG"))


def log(ctx, msg):
    if DBG and ctx is not None:
        try:
            ctx.job_queue.run_once(
                lambda _c: None, 0)  # no-op; keeps job_queue referenced
        except Exception:
            pass
    print("[relay] %s" % msg, flush=True)


# The site's OCR runs in the page, so the browser is the expensive part: ~640MB
# measured. On a 1GB host it must not be held open. One Relay at a time, and
# only for as long as a Screenshot is in flight.
_site_lock = asyncio.Lock()
_relay = None


def get_relay(progress=None) -> driver.Relay:
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
async def browser_session(progress=None):
    """Yield a started Relay, then close the browser.

    Transient by design: the browser exists for the duration of one Screenshot
    and is torn down afterwards, so idle RAM is the bot process alone.

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
        print("[relay] launching browser (%.0fMB free)" % rep["available_mb"],
              flush=True)
        try:
            await asyncio.to_thread(r.start)
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
                print("[relay] %.0fMB available after close" % mem_after,
                      flush=True)


async def site_login(ctx, force=False):
    """Sign in using the credentials held for this chat.

    The old ledger "session_valid" shortcut is gone: it was never written to, so
    it always reported stale, and with prompted credentials the browser profile
    is what actually decides whether a re-login is needed.
    """
    raise RuntimeError("site_login(ctx) is superseded by browser_session()")


async def sign_in(chat_id, progress=None):
    """Sign the Relay in for this chat, or raise something we can explain.

    Called inside an open browser_session(), so the caller owns the browser
    lifetime; this only resolves the credentials and logs in.

    `progress` is threaded through rather than read off the Relay so a caller
    cannot silently get a reporter that belongs to an earlier Screenshot.
    """
    r = get_relay(progress)
    username, password = prompts_mod.site_credentials(chat_id)
    await asyncio.to_thread(r.login, username, password)
    return r
