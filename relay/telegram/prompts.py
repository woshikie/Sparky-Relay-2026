"""The Credentials Prompt: getting the Site username and password.

A chat moves through stages -- choose_preset, username, password, ready --
held in PROMPTING with a TTL, because the answer arrives in a later message
than the question. kb_for() is the single decision point for which keyboard a
chat sees, so no path can send a keyboard that does not fit the prompt.
"""

import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError

import relay.store.ledger as ledger
import relay.store.vault as vault
import relay.telegram.access as access
import relay.telegram.words as words
from relay import config
from relay.telegram.keyboards import kb_credential_choice, kb_prompt, kb_reply


class _NoCredentials(Exception):
    """No Site credentials supplied for this chat yet."""


def _credential_prompt_body():
    """The prompt text, without choosing a keyboard for it."""
    if config.has_preset_credentials():
        return words.choose_preset(config.SITE_USERNAME)
    return words.ask_username()


def _start_credential_stage(chat_id):
    if config.has_preset_credentials():
        _set_stage(chat_id, "choose_preset")
    else:
        _set_stage(chat_id, "username")


def has_credentials(chat_id):
    """True if this chat can sign in right now."""
    st = _prompt_stage(chat_id) or {}
    if st.get("preset"):
        return config.has_preset_credentials()
    return ledger.credentials_stored(chat_id) is not None


async def on_login(update: Update, ctx):
    """(Re-)supply Site credentials at any time."""
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d), parse_mode=ParseMode.MARKDOWN)
        return
    _clear_stage(chat.id)
    await ask_credentials(msg, chat.id)


async def on_logout(update: Update, ctx):
    """Drop the stored credentials and any prompt in progress."""
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d), parse_mode=ParseMode.MARKDOWN)
        return
    _clear_stage(chat.id)
    ledger.forget_credentials(chat.id)
    await msg.reply_text(
        words.logged_out(), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_for(chat.id)
    )


# ----------------------------------------------------------------------
# Credentials Prompt
# ----------------------------------------------------------------------

# Where a chat is in the Credentials Prompt, or the Shared Secret exchange.
# chat_id -> {"stage": ..., "preset": bool}
PROMPTING = {}

# How long a half-finished prompt is worth holding before forgetting it.
PROMPT_TTL = 600.0


def _prompt_stage(chat_id):
    st = PROMPTING.get(chat_id)
    if st and time.time() - st.get("at", 0) > PROMPT_TTL:
        PROMPTING.pop(chat_id, None)
        return None
    return st


def _set_stage(chat_id, stage, **kw):
    PROMPTING[chat_id] = dict(stage=stage, at=time.time(), **kw)
    return PROMPTING[chat_id]


def _clear_stage(chat_id):
    return PROMPTING.pop(chat_id, None)


async def ask_credentials(msg, chat_id):
    """Offer Preset Credentials if they exist, otherwise just ask."""
    if config.has_preset_credentials():
        _set_stage(chat_id, "choose_preset")
        await msg.reply_text(
            words.choose_preset(config.SITE_USERNAME),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat_id),
        )
        return
    await ask_username(msg, chat_id)


async def ask_username(msg, chat_id):
    _set_stage(chat_id, "username")
    await msg.reply_text(
        words.ask_username(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat_id),
    )


async def ask_password(msg, chat_id, username=None, preset=False):
    """Move to the password step, carrying the username forward.

    The username has to survive the transition: it arrived in the previous
    message and exists nowhere else by the time the password lands. _set_stage
    replaces the whole dict rather than merging, so it is passed explicitly --
    an earlier version dropped it here and the password could not be stored.
    """
    _set_stage(chat_id, "password", username=username, preset=preset)
    await msg.reply_text(
        words.ask_password(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat_id),
    )


def kb_for(chat_id):
    """The keyboard this chat needs right now."""
    stage = _prompt_stage(chat_id)
    if not stage:
        return kb_reply()
    name = stage.get("stage")
    if name == "choose_preset":
        return kb_credential_choice()
    if name in ("username", "password"):
        return kb_prompt()
    return kb_reply()


async def on_credential_choice(update: Update, ctx):
    """Callback path for the preset choice.

    The choice is now a reply keyboard, so this is only reachable from the
    `kb_after_login` escape hatch on an older message still sitting in the chat.
    Kept, because Telegram keeps inline buttons alive on old messages, and a tap
    that did nothing would be worse than a slightly redundant handler.
    """
    q = update.callback_query
    chat = update.effective_chat
    st = _prompt_stage(chat.id)
    if not st or st.get("stage") != "choose_preset":
        await q.answer("Nothing to choose — tap Sign in first.", show_alert=True)
        return
    if q.data.endswith("preset"):
        # Credentials came from the environment: nothing to store, and the
        # vault is not involved at all.
        _clear_stage(chat.id)
        _set_stage(chat.id, "ready", username=config.SITE_USERNAME, preset=True)
        await q.edit_message_text(
            words.using_preset(config.SITE_USERNAME), parse_mode=ParseMode.MARKDOWN
        )
        await q.message.reply_text(
            words.send_a_screenshot(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        await q.answer("using preset credentials")
    else:
        await q.edit_message_reply_markup(reply_markup=None)
        await ask_username(q.message, chat.id)
        await q.answer()


async def _scrub(msg):
    """Delete a message that carried a secret. Best effort.

    A bot can delete messages in a chat it administers, which covers private
    chats. It cannot undo a notification that already rendered, so the README
    says to use a private chat for anything sensitive.
    """
    try:
        await msg.delete()
    except TelegramError:
        try:
            await msg.reply_text(words.scrub_failed(), parse_mode=ParseMode.MARKDOWN)
            await msg.delete()
        except TelegramError:
            pass


def site_credentials(chat_id):
    """(username, password) for a chat.

    Raises _NoCredentials when the chat has not supplied any, and
    vault.DecryptionFailed when the stored copy cannot be opened (normally
    because the bot token was rotated). Those are different failures and get
    different replies: "send /login" versus "your stored password is gone,
    send it again".

    Returning None here instead would surface as a TypeError from the tuple
    unpacking in sign_in, which is not a message anyone can act on.
    """
    st = _prompt_stage(chat_id) or {}
    if st.get("preset"):
        if config.has_preset_credentials():
            return config.SITE_USERNAME, config.SITE_PASSWORD
        raise vault.DecryptionFailed("preset credentials are no longer configured")
    creds = ledger.load_credentials(chat_id, config.TELEGRAM_BOT_TOKEN)
    if not creds:
        raise _NoCredentials("no credentials stored for this chat")
    return creds


# ----------------------------------------------------------------------
