"""One mapping from browser-driving failure to user reply.

on_photo, on_sync, and cb_ok used to carry near-identical six-branch except
chains. They differed only in cleanup -- deleting a scratch message, dropping
a pending request, answering a callback -- so the mapping lived in three
places and could only drift. Now the mapping lives here, and each caller does
its own cleanup before calling in:

    except Exception as e:
        await prog.stop()
        await scratch.delete()
        alert, alarm, log_line = await failures.explain(
            msg, e, operation="Upload",
            start_prompt=lambda: ask_credentials(msg, chat.id))
        if log_line:
            session_mod.log(ctx, log_line)

Return:
`operation` names the step for the catch-all ("Upload", "Sync", "Commit") and
for the log line. `start_prompt` is a zero-arg coroutine that begins the
Credentials Prompt, used by the two failures that are fixed by supplying
credentials. The returned triple is the callback answer: text (or None for a
silent ack), whether it shows as an alert, and a log line (or None).

"""

from collections.abc import Awaitable, Callable
from typing import Literal, NamedTuple

from telegram import Message
from telegram.constants import ParseMode

from relay import memory
from relay.site import driver as relay_site
from relay.store import vault as vault_mod
from relay.telegram import words as words_mod
from relay.telegram.prompts import _NoCredentials

# The three flows that can fail into this mapping. Closed, not free text: a
# fourth caller inventing its own headline must add it here, where the
# headlines live, rather than silently borrowing the Commit copy.
Operation = Literal["Upload", "Sync", "Commit"]


class FailureKind(NamedTuple):
    """What a failure means, without sending anything.

    Pure data, computed by classify(): the reply text, the callback answer
    (text or None for a silent ack, whether it shows as an alert), the log
    line (only the catch-all logs), and whether supplying credentials would
    fix it (only then does the prompt start).
    """

    reply: str
    alert: str | None
    alarm: bool
    log_line: str | None
    prompt: bool


# The catch-all keeps each caller's own headline: the words are the same shape
# ("X failed"), but the copy was written per flow and there is no reason to
# churn what the user reads in a refactor.
# The headlines keep each caller's own text byte-for-byte -- including the
# Sync headline, which has no emoji where the other two do. Unifying the copy
# is a separate change; a refactor must not churn what the user reads.
CATCH_ALL: dict[Operation, str] = {
    "Upload": "❌ Upload failed: `%s`",
    "Sync": " Sync failed: `%s`",
    "Commit": "❌ Could not record: `%s`\n\nNothing was saved — send the "
    "screenshot again when ready.",
}


def _need_credentials(_exc: BaseException) -> FailureKind:
    return FailureKind(
        reply=words_mod.need_credentials(),
        alert="credentials needed",
        alarm=True,
        log_line=None,
        prompt=True,
    )


def _vault_unreadable(exc: BaseException) -> FailureKind:
    # Almost always a rotated bot token: the vault key no longer opens the
    # stored entry. The old password is unrecoverable by design.
    return FailureKind(
        reply=words_mod.vault_unreadable(str(exc)),
        alert="stored credentials unreadable",
        alarm=True,
        log_line=None,
        prompt=True,
    )


def _low_memory(exc: BaseException) -> FailureKind:
    return FailureKind(
        reply=words_mod.low_memory(str(exc)),
        alert="not enough memory",
        alarm=True,
        log_line=None,
        prompt=False,
    )


def _no_steps(_exc: BaseException) -> FailureKind:
    return FailureKind(
        reply=words_mod.no_steps(),
        alert="the site read nothing this time",
        alarm=True,
        log_line=None,
        prompt=False,
    )


def _site_changed(exc: BaseException) -> FailureKind:
    return FailureKind(
        reply=words_mod.site_changed(str(exc)),
        alert=None,
        alarm=False,
        log_line=None,
        prompt=False,
    )


# The recognised mapping, as a table: exception type to meaning. Order is
# insertion order, and none of the five is a subclass of another, so no
# branch can shadow one above it.
_CLASSIFY: dict[type[BaseException], Callable[[BaseException], FailureKind]] = {
    _NoCredentials: _need_credentials,
    vault_mod.DecryptionFailed: _vault_unreadable,
    memory.InsufficientMemory: _low_memory,
    relay_site.NoStepsFound: _no_steps,
    relay_site.SiteChanged: _site_changed,
}


def _catch_all(exc: BaseException, operation: Operation) -> FailureKind:
    try:
        headline = CATCH_ALL[operation]
    except KeyError:
        raise ValueError("unknown operation: %r" % (operation,)) from None
    return FailureKind(
        reply=headline % str(exc)[:200],
        alert=None,
        alarm=False,
        log_line="%s error: %r" % (operation.lower(), exc),
        prompt=False,
    )


def classify(exc: BaseException, operation: Operation) -> FailureKind:
    """Map a failure to its meaning, without sending anything.

    Pure: no Message, no awaits, no network. The parametrized test below is
    literally this table's rows, so a change to what any failure means is a
    deliberate edit there rather than drift.
    """
    for cls, build in _CLASSIFY.items():
        if isinstance(exc, cls):
            return build(exc)
    return _catch_all(exc, operation)


async def explain(
    msg: Message,
    exc: BaseException,
    operation: Operation,
    start_prompt: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str | None, bool, str | None]:
    """Tell the user what went wrong. Never raises for a recognised failure.

    Returns (alert, alarm, log_line). Only the catch-all produces a log line;
    the five recognised failures are routine enough to say out loud and leave
    out of the log. An unknown operation raises ValueError: the three callers
    are the whole set, and a typo must fail loudly rather than borrow the
    Commit copy.
    """
    kind = classify(exc, operation)
    await msg.reply_text(kind.reply, parse_mode=ParseMode.MARKDOWN)
    if kind.prompt and start_prompt is not None:
        await start_prompt()
    return kind.alert, kind.alarm, kind.log_line
