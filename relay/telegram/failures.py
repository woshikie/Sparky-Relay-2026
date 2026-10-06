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
from telegram.constants import ParseMode

from relay import memory
from relay.site import driver as relay_site
from relay.store import vault as vault_mod
from relay.telegram import words as words_mod
from relay.telegram.prompts import _NoCredentials

# The catch-all keeps each caller's own headline: the words are the same shape
# ("X failed"), but the copy was written per flow and there is no reason to
# churn what the user reads in a refactor.
# The headlines keep each caller's own text byte-for-byte -- including the
# Sync headline, which has no emoji where the other two do. Unifying the copy
# is a separate change; a refactor must not churn what the user reads.
CATCH_ALL = {
    "Upload": "❌ Upload failed: `%s`",
    "Sync": " Sync failed: `%s`",
    "Commit": "❌ Could not record: `%s`\n\nNothing was saved — send the "
              "screenshot again when ready.",
}


async def explain(msg, exc, operation, start_prompt=None):
    """Tell the user what went wrong. Never raises for a recognised failure.

    Returns (alert, alarm, log_line). Only the catch-all produces a log line;
    the five recognised failures are routine enough to say out loud and leave
    out of the log.
    """
    if isinstance(exc, _NoCredentials):
        await msg.reply_text(words_mod.need_credentials(),
                             parse_mode=ParseMode.MARKDOWN)
        if start_prompt is not None:
            await start_prompt()
        return "credentials needed", True, None
    if isinstance(exc, vault_mod.DecryptionFailed):
        # Almost always a rotated bot token: the vault key no longer opens the
        # stored entry. The old password is unrecoverable by design.
        await msg.reply_text(words_mod.vault_unreadable(str(exc)),
                             parse_mode=ParseMode.MARKDOWN)
        if start_prompt is not None:
            await start_prompt()
        return "stored credentials unreadable", True, None
    if isinstance(exc, memory.InsufficientMemory):
        await msg.reply_text(words_mod.low_memory(str(exc)),
                             parse_mode=ParseMode.MARKDOWN)
        return "not enough memory", True, None
    if isinstance(exc, relay_site.NoStepsFound):
        await msg.reply_text(words_mod.no_steps(), parse_mode=ParseMode.MARKDOWN)
        return "the site read nothing this time", True, None
    if isinstance(exc, relay_site.SiteChanged):
        await msg.reply_text(words_mod.site_changed(str(exc)),
                             parse_mode=ParseMode.MARKDOWN)
        return None, False, None
    headline = CATCH_ALL.get(operation, CATCH_ALL["Commit"])
    await msg.reply_text(headline % str(exc)[:200],
                         parse_mode=ParseMode.MARKDOWN)
    return None, False, "%s error: %r" % (operation.lower(), exc)
