"""The Credentials Prompt: getting the Site username and password.

A chat moves through stages -- choose_preset, username, password, ready --
held in PROMPTING with a TTL, because the answer arrives in a later message
than the question. kb_for() is the single decision point for which keyboard a
chat sees, so no path can send a keyboard that does not fit the prompt.
"""

import time
from typing import Any

from telegram import Message, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import relay.store.ledger as ledger
import relay.store.vault as vault
import relay.telegram.access as access
import relay.telegram.words as words
from relay import config
from relay.errors import NoCredentials
from relay.telegram.keyboards import kb_credential_choice, kb_prompt, kb_reply


def credential_prompt_body() -> str:
    """The prompt text, without choosing a keyboard for it."""
    if config.has_preset_credentials():
        return words.choose_preset(config.SITE_USERNAME)
    return words.ask_username()


def start_credential_stage(chat_id: int) -> None:
    if config.has_preset_credentials():
        set_stage(chat_id, "choose_preset")
    else:
        set_stage(chat_id, "username")


def has_credentials(chat_id: int) -> bool:
    """True if this chat can sign in right now."""
    st = prompt_stage(chat_id) or {}
    if st.get("preset") or (
        not st and config.has_preset_credentials() and ledger.has_preset_choice(chat_id)
    ):
        return config.has_preset_credentials()
    return ledger.credentials_stored(chat_id) is not None


async def on_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """(Re-)supply Site credentials at any time."""
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return
    clear_stage(chat.id)
    await ask_credentials(msg, chat.id)


async def on_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """Drop the stored credentials and any prompt in progress."""
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return
    clear_stage(chat.id)
    ledger.forget_credentials(chat.id)
    ledger.forget_preset_choice(chat.id)
    await msg.reply_text(
        words.logged_out(), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_for(chat.id)
    )


# ----------------------------------------------------------------------
# Credentials Prompt
# ----------------------------------------------------------------------

# Where a chat is in the Credentials Prompt, or the Shared Secret exchange.
# chat_id -> {"stage": ..., "preset": ...}. A plain stringly-typed bag:
# the keys come and go by stage, which is exactly what prompt_stage and
# set_stage exist to contain, so a TypedDict would fight the **kw below.
PROMPTING: dict[int, dict[str, Any]] = {}

# How long a half-finished prompt is worth holding before forgetting it.
PROMPT_TTL = 600.0


def prompt_stage(chat_id: int) -> dict[str, Any] | None:
    st = PROMPTING.get(chat_id)
    if st and time.time() - st.get("at", 0) > PROMPT_TTL:
        PROMPTING.pop(chat_id, None)
        return None
    return st


def set_stage(chat_id: int, stage: str, **kw: object) -> dict[str, Any]:
    PROMPTING[chat_id] = dict(stage=stage, at=time.time(), **kw)
    return PROMPTING[chat_id]


def clear_stage(chat_id: int) -> dict[str, Any] | None:
    return PROMPTING.pop(chat_id, None)


async def ask_credentials(msg: Message, chat_id: int) -> None:
    """Offer Preset Credentials if they exist, otherwise just ask."""
    if config.has_preset_credentials():
        set_stage(chat_id, "choose_preset")
        await msg.reply_text(
            words.choose_preset(config.SITE_USERNAME),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat_id),
        )
        return
    await ask_username(msg, chat_id)


async def ask_username(msg: Message, chat_id: int) -> None:
    set_stage(chat_id, "username")
    await msg.reply_text(
        words.ask_username(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat_id),
    )


async def ask_password(
    msg: Message, chat_id: int, username: str | None = None, preset: bool = False
) -> None:
    """Move to the password step, carrying the username forward.

    The username has to survive the transition: it arrived in the previous
    message and exists nowhere else by the time the password lands. set_stage
    replaces the whole dict rather than merging, so it is passed explicitly --
    an earlier version dropped it here and the password could not be stored.
    """
    set_stage(chat_id, "password", username=username, preset=preset)
    await msg.reply_text(
        words.ask_password(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat_id),
    )


async def refuse(msg: Message, decision: access.Decision) -> None:
    """Tell a chat it cannot drive the Relay, and why.

    The shared shape of every access refusal: the decision already knows
    the reason, words already knows the copy. Handlers that need more
    (on_start's claim/secret branches, with their keyboards) keep their
    bespoke replies.
    """
    await msg.reply_text(words.access_refused(decision), parse_mode=ParseMode.MARKDOWN)


def kb_for(chat_id: int) -> ReplyKeyboardMarkup:
    """The keyboard this chat needs right now."""
    stage = prompt_stage(chat_id)
    if not stage:
        return kb_reply()
    name = stage.get("stage")
    if name == "choose_preset":
        return kb_credential_choice()
    if name in ("username", "password"):
        return kb_prompt()
    return kb_reply()


async def on_credential_choice(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE | None
) -> None:
    """Callback path for the preset choice.

    The choice is now a reply keyboard, so this is only reachable from the
    `kb_after_login` escape hatch on an older message still sitting in the chat.
    Kept, because Telegram keeps inline buttons alive on old messages, and a tap
    that did nothing would be worse than a slightly redundant handler.
    """
    q = update.callback_query
    chat = update.effective_chat
    msg = update.effective_message
    assert q is not None and chat is not None and msg is not None
    st = prompt_stage(chat.id)
    if not st or st.get("stage") != "choose_preset":
        await q.answer("Nothing to choose — tap Sign in first.", show_alert=True)
        return
    data = q.data or ""
    if data.endswith("preset"):
        # Credentials came from the environment: nothing to store, and the
        # vault is not involved at all.
        clear_stage(chat.id)
        set_stage(chat.id, "ready", username=config.SITE_USERNAME, preset=True)
        ledger.note_preset_choice(chat.id)
        await q.edit_message_text(
            words.using_preset(config.SITE_USERNAME), parse_mode=ParseMode.MARKDOWN
        )
        await msg.reply_text(
            words.send_a_screenshot(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat.id),
        )
        await q.answer("using preset credentials")
    else:
        await q.edit_message_reply_markup(reply_markup=None)
        await ask_username(msg, chat.id)
        await q.answer()


async def scrub(msg: Message) -> None:
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


def site_credentials(chat_id: int) -> tuple[str, str]:
    """(username, password) for a chat.

    Raises NoCredentials when the chat has not supplied any, and
    vault.DecryptionFailed when the stored copy cannot be opened (normally
    because the bot token was rotated). Those are different failures and get
    different replies: "send /login" versus "your stored password is gone,
    send it again".

    Returning None here instead would surface as a TypeError from the tuple
    unpacking in sign_in, which is not a message anyone can act on.
    """
    st = prompt_stage(chat_id) or {}
    if st.get("preset") or (
        not st and config.has_preset_credentials() and ledger.has_preset_choice(chat_id)
    ):
        if config.has_preset_credentials():
            return config.SITE_USERNAME, config.SITE_PASSWORD
        raise vault.DecryptionFailed("preset credentials are no longer configured")
    creds = ledger.load_credentials(chat_id, config.TELEGRAM_BOT_TOKEN)
    if not creds:
        raise NoCredentials("no credentials stored for this chat")
    return creds


# ----------------------------------------------------------------------
