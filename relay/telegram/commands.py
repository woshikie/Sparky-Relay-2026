"""Commands and the text router.

on_text lives here rather than in prompts.py so the dependency points one
way: the router dispatches to prompt stages and to command handlers alike,
and sitting next to the handlers avoids a prompts<->commands cycle. The
nightly backup lives in store/backup.py, next to the ledger it dumps.
"""

import asyncio
from contextlib import suppress
from typing import Any, cast

from telegram import Chat, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import relay.store.ledger as ledger
import relay.telegram.access as access
import relay.telegram.failures as failures
import relay.telegram.pending as pending_mod
import relay.telegram.session as session_mod
import relay.telegram.words as words
from relay import config, memory
from relay import progress as progress_mod
from relay.telegram.keyboards import button_actions, kb_after_login, kb_reply
from relay.telegram.prompts import (
    ask_credentials,
    ask_password,
    clear_stage,
    credential_prompt_body,
    has_credentials,
    kb_for,
    on_login,
    on_logout,
    prompt_stage,
    refuse,
    scrub,
    set_stage,
    start_credential_stage,
)

# ----------------------------------------------------------------------


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """Entry point. Access Mode decides whether anything else happens.

    Sends exactly one message. The greeting and the Credentials Prompt used to
    be separate replies, which read as the bot talking to itself; the prompt now
    rides along on the same message, with the reply keyboard attached so the
    commands are one tap away.
    """
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))

    if not d:
        if d.why == "needs_claim":
            # First-run claim: whoever got here first owns the Relay.
            if access.claim(chat.id, getattr(chat, "username", None)):
                await msg.reply_text(
                    words.claimed(),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_reply(),
                )
            else:
                await msg.reply_text(
                    words.already_claimed(),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_reply(),
                )
            return
        if d.why == "needs_secret":
            await msg.reply_text(
                words.secret_prompt(),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_reply(),
            )
            return
        await msg.reply_text(
            words.access_refused(d),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_reply(),
        )
        return

    if has_credentials(chat.id):
        body = words.welcome(getattr(chat, "first_name", None))
    else:
        # One message: the greeting, then the prompt. The keyboard below it is
        # whatever this chat needs *now*, so the commands are never more than
        # one tap away and the prompt is answered by tapping, not typing.
        body = (
            words.welcome(getattr(chat, "first_name", None))
            + "\n\n"
            + credential_prompt_body()
        )
        start_credential_stage(chat.id)
    await msg.reply_text(
        body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_for(chat.id)
    )


async def on_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return
    await msg.reply_text(
        words.log_lines(ledger.all_submissions()),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat.id),
    )


async def on_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """Read the site's own record and store it. Submits nothing.

    The point is the overwrite guard. A day the user entered by hand is real
    and counts, but the bot has no record of it, so without a sync the guard
    would treat it as a fresh day and overwrite it -- including downwards,
    which is the one direction the guard exists to challenge.

    Read-only by construction: it calls read_days(), which navigates and reads
    and never touches the upload form.
    """
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return
    if not has_credentials(chat.id):
        await msg.reply_text(words.need_credentials(), parse_mode=ParseMode.MARKDOWN)
        await ask_credentials(msg, chat.id)
        return

    scratch = await msg.reply_text(words.syncing())
    prog = await progress_mod.Progress(scratch).start()
    try:
        async with session_mod.browser_session(chat.id, prog) as r:
            days = await asyncio.to_thread(r.read_days)
    except Exception as e:
        await prog.stop()
        await scratch.delete()
        _alert, _alarm, log_line = await failures.explain(
            msg, e, operation="Sync", start_prompt=lambda: ask_credentials(msg, chat.id)
        )
        if log_line:
            session_mod.log(ctx, log_line)
        return

    if not days:
        # Say so rather than reporting a successful sync of nothing, which
        # would look identical to a site with no days on it.
        await prog.close(words.sync_empty(), parse_mode=ParseMode.MARKDOWN)
        return

    written = ledger.record_site_days(days)
    await prog.close(words.sync_report(days, written), parse_mode=ParseMode.MARKDOWN)


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return
    subs = ledger.all_submissions()
    rep = memory.budget()
    creds = ledger.credentials_stored(chat.id)
    st = prompt_stage(chat.id) or {}
    preset_in_use = bool(st.get("preset")) and config.has_preset_credentials()

    if preset_in_use:
        who = "%s (preset from config)" % words.code(config.SITE_USERNAME)
    elif creds:
        who = "%s (stored, encrypted)" % words.code(creds["username"])
    else:
        who = "none — send /login"

    granted = ledger.all_access()
    denied = ledger.all_denied()
    txt = (
        "**Relay status**\n\n"
        "**Site:** %s\n"
        "**Signing in as:** %s\n"
        # access.describe() carries its own ** markers, and nesting those inside
        # another ** pair made the whole reply unparseable. Plain fragment.
        "**Access:** %s\n"
        "**Browser:** headless=%s, launched per Screenshot\n"
        "**Memory:** %.0fMB usable (browser needs ~%dMB) — can launch: **%s**\n"
        "**Submissions recorded:** %d\n"
        "**Chats with access:** %s\n"
        "**Chats denied:** %s\n"
        "**Pending confirmations:** %d"
        % (
            words.code(config.SITE_BASE),
            who,
            access.describe(markdown=False),
            config.HEADLESS,
            rep["available_mb"] or 0,
            rep["browser_peak_mb"] or 0,
            "yes" if rep["can_launch"] else "NO",
            len(subs),
            ", ".join(
                "%s (%s)" % (words.code(a["chat_id"]), words.md(a["how"]))
                for a in granted
            )
            or "none",
            ", ".join(words.code(a["chat_id"]) for a in denied) or "none",
            pending_mod.count(),
        )
    )
    # Refresh the keyboard so the buttons Telegram is showing match what the
    # bot can actually do right now.
    await msg.reply_text(
        txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_for(chat.id)
    )


async def _run_button(
    ctx: ContextTypes.DEFAULT_TYPE | None,
    msg: Message,
    chat: Chat,
    action: str,
    stage: dict[str, Any] | None,
) -> None:
    """Dispatch a tapped reply-keyboard button."""
    if action == "submit":
        if stage and stage.get("stage") in ("username", "password"):
            await msg.reply_text(
                words.cancel_prompt_first(),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_for(chat.id),
            )
            return
        await msg.reply_text(
            words.send_a_screenshot(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        return

    if action == "use_preset":
        clear_stage(chat.id)
        set_stage(chat.id, "ready", username=config.SITE_USERNAME, preset=True)
        await msg.reply_text(
            words.using_preset(config.SITE_USERNAME),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        return

    if action == "new_creds":
        set_stage(chat.id, "username")
        await msg.reply_text(
            words.ask_username(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        return

    if action == "cancel":
        clear_stage(chat.id)
        await msg.reply_text(
            words.cancelled(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        return

    handler = {
        "login": on_login,
        "logout": on_logout,
        "status": on_status,
        "log": on_log,
        "help": on_start,
        "sync": on_sync,
    }.get(action)
    if handler is None:
        return
    # Re-enter through the command handlers so there is one implementation of
    # each, not a second copy here that could fall behind.
    await handler(_as_update(msg, chat), ctx)
    # A tapped command should leave the keyboard showing what comes next,
    # which for /login is the prompt rather than the standing commands.
    if stage is None:
        pending = prompt_stage(chat.id)
        if pending and pending.get("stage") in ("username", "password"):
            with suppress(TelegramError):
                await msg.reply_text(
                    words.ask_password()
                    if pending.get("stage") == "password"
                    else words.ask_username(),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_for(chat.id),
                )


def _as_update(msg: Message, chat: Chat) -> Update:
    update = Update(0, message=msg)
    update._effective_chat = chat
    # effective_user stays None: no handler reads it, and a Chat is not a
    # User. (mypy caught the original line claiming otherwise.)
    return update


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """Handle the Credentials Prompt, the Shared Secret exchange, and buttons.

    Reply-keyboard buttons arrive here as ordinary text, so they are dispatched
    first. That has to happen before the prompt handling: mid-prompt the bot
    expects a username or a password, and a tapped "Submit steps" button must
    not be mistaken for either.
    """
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    text = (msg.text or "").strip()
    st = prompt_stage(chat.id)

    if text in button_actions():
        await _run_button(ctx, msg, chat, button_actions()[text], st)
        return

    # --- Access gate first: an unauthorised chat gets nothing, and its input
    # --- is never treated as a secret.
    d = access.check(chat.id, getattr(chat, "username", None))
    if d.why in ("needs_secret", "secret_throttled"):
        if not text:
            return
        if d.why == "secret_throttled":
            # Still treat this as a secret attempt, so the message carrying it
            # gets deleted, then say why it did not work. Falling through to
            # the "nothing to do" reply instead would leave a password sitting
            # in the chat with no explanation.
            await scrub(msg)
            await msg.reply_text(
                words.secret_throttled(d.retry_after), parse_mode=ParseMode.MARKDOWN
            )
            return
        # The rate limit is enforced inside present_secret, not here.
        r = access.present_secret(chat.id, text, getattr(chat, "username", None))
        # The message carried a secret: remove it from the chat immediately.
        await scrub(msg)
        if r:
            await msg.reply_text(
                words.secret_accepted(r.how), parse_mode=ParseMode.MARKDOWN
            )
            await ask_credentials(msg, chat.id)
        elif r.why == "secret_throttled":
            await msg.reply_text(
                words.secret_throttled(r.retry_after), parse_mode=ParseMode.MARKDOWN
            )
        else:
            await msg.reply_text(words.secret_rejected(), parse_mode=ParseMode.MARKDOWN)
        return

    if not st:
        # No prompt in progress and access is fine: treat as an unknown
        # command rather than silently swallowing it.
        await msg.reply_text(words.no_prompt(), parse_mode=ParseMode.MARKDOWN)
        return

    if st.get("stage") == "username":
        if not text:
            return
        if len(text) < 3 or len(text) > 40:
            await msg.reply_text(words.bad_username(), parse_mode=ParseMode.MARKDOWN)
            return
        await ask_password(msg, chat.id, username=text, preset=bool(st.get("preset")))
        return

    if st.get("stage") == "password":
        if not text:
            return
        # The password stage is unreachable without the username stage first,
        # so this is str at runtime; the cast says so once, at the boundary.
        username = cast(str, st.get("username"))
        # Store encrypted, then drop it. The plaintext is not kept anywhere.
        try:
            ledger.save_credentials(
                chat.id, username, text, config.TELEGRAM_BOT_TOKEN, preset=False
            )
        except Exception as e:
            await msg.reply_text(
                words.credential_store_failed(str(e)[:120]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        finally:
            clear_stage(chat.id)
        await scrub(msg)
        set_stage(chat.id, "ready", username=username, preset=False)
        await msg.reply_text(
            words.credentials_saved(username),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_after_login(),
        )
        return
