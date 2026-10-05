"""The reply keyboard, and the buttons that arrive through it.

A reply keyboard is plain text over the message box, so its labels reach the bot
as ordinary messages. That is the whole reason for two decisions worth testing:
the buttons are dispatched inside `on_text` rather than as a second handler
(python-telegram-bot stops at the first match within a group), and a tapped
button is resolved before the Credentials Prompt, so "Submit steps" mid-prompt
cannot be read as a password.
"""
import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def keyboard_bot(bot, access, ledger):
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)
    return bot


# --------------------------------------------------------------- the layout

def test_the_keyboard_offers_the_standing_commands(keyboard_bot):
    kb = keyboard_bot.kb_reply()
    labels = [b.text for row in kb.keyboard for b in row]
    for expected in ("/login", "/logout", "/status", "/log", "/help"):
        assert any(expected in l for l in labels), expected


def test_every_keyboard_label_has_a_handler(keyboard_bot):
    """A label with no entry in BUTTON_COMMANDS is a button that does nothing."""
    labels = [b.text for row in keyboard_bot.kb_reply().keyboard for b in row]
    for label in labels:
        assert label in keyboard_bot.BUTTON_COMMANDS, label


def test_every_handler_has_a_keyboard_label(keyboard_bot):
    """The reverse: a command in the map that no button can reach."""
    labels = {b.text for row in keyboard_bot.kb_reply().keyboard for b in row}
    for label in keyboard_bot.BUTTON_COMMANDS:
        if label == "❌ Cancel":
            continue          # shown only mid-prompt, by kb_prompt()
        assert label in labels, label


def test_the_cancel_button_is_on_the_prompt_keyboard(keyboard_bot):
    labels = [b.text for row in keyboard_bot.kb_prompt().keyboard for b in row]
    cancel_label = next(k for k, v in keyboard_bot.BUTTON_COMMANDS.items()
                        if v == "cancel")
    assert cancel_label in labels


def test_the_keyboard_is_persistent(keyboard_bot):
    """It should survive the user restarting the Telegram client."""
    assert keyboard_bot.kb_reply().is_persistent is True


def test_the_prompt_keyboard_is_one_time(keyboard_bot):
    """Mid-prompt the user has to type, so the keys should get out of the way."""
    assert keyboard_bot.kb_prompt().one_time_keyboard is True


def test_the_keyboard_resizes_to_its_content(keyboard_bot):
    assert keyboard_bot.kb_reply().resize_keyboard is True


# ------------------------------------------------------------- the dispatch

class Btn:
    def __init__(self, chat_id=1, text="/status"):
        self.chat = type("C", (), {"id": chat_id, "username": "u",
                                   "first_name": "W"})()
        self.message_id = 1
        self.text = text
        self.replies = []
        self.edits = []
        self.deleted = False
        self.photo = None
        self.document = None
        self.reply_to_message = None

    @property
    def effective_chat(self):
        return self.chat

    @property
    def effective_message(self):
        return self

    @property
    def said(self):
        return " ".join(t for t, _ in self.replies)

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))
        return self

    async def delete(self):
        self.deleted = True


class Upd:
    def __init__(self, chat_id=1, text="/status"):
        self.message = Btn(chat_id, text)
        self.effective_chat = self.message.chat
        self.effective_message = self.message
        self.callback_query = None


def press(bot, text, chat_id=1):
    upd = Upd(chat_id, text)
    run(bot.on_text(upd, None))
    return upd


def test_the_status_button_answers(keyboard_bot):
    upd = press(keyboard_bot, "📋 /status")
    assert "Relay status" in upd.message.said


def test_the_log_button_answers(keyboard_bot):
    upd = press(keyboard_bot, "📜 /log")
    assert upd.message.said


def test_the_login_button_starts_the_prompt(keyboard_bot):
    upd = press(keyboard_bot, "🔑 /login")
    assert keyboard_bot._prompt_stage(1)["stage"] in ("username", "choose_preset")


def test_the_logout_button_forgets(keyboard_bot, ledger):
    press(keyboard_bot, "🚪 /logout")
    assert ledger.credentials_stored(1) is None


def test_the_help_button_greets(keyboard_bot):
    upd = press(keyboard_bot, "❓ /help")
    assert "Hello" in upd.message.said


def test_the_submit_button_asks_for_a_screenshot(keyboard_bot):
    upd = press(keyboard_bot, "📸 Submit steps")
    assert "screenshot" in upd.message.said.lower()


def test_the_cancel_button_stops_a_prompt(keyboard_bot):
    press(keyboard_bot, "🔑 /login")
    assert keyboard_bot._prompt_stage(1) is not None
    upd = press(keyboard_bot, "❌ Cancel")
    assert keyboard_bot._prompt_stage(1) is None
    assert "recorded" in upd.message.said.lower()


# --------------------------------------------------------- the ordering rule

def test_a_button_is_not_mistaken_for_an_answer(keyboard_bot):
    """The regression: mid-prompt, "Submit steps" must not become a username."""
    press(keyboard_bot, "🔑 /login")
    stage = keyboard_bot._prompt_stage(1)["stage"]
    press(keyboard_bot, "📸 Submit steps")
    # The prompt is still pending: the tap did not fill it in.
    assert keyboard_bot._prompt_stage(1)["stage"] == stage


def test_a_button_mid_prompt_says_what_to_do(keyboard_bot):
    press(keyboard_bot, "🔑 /login")
    upd = press(keyboard_bot, "📸 Submit steps")
    assert "part-way" in upd.message.said or "Finish" in upd.message.said


def test_free_text_still_reaches_the_prompt(keyboard_bot):
    """A button must not swallow a real username."""
    press(keyboard_bot, "🔑 /login")
    if keyboard_bot._prompt_stage(1)["stage"] == "username":
        press(keyboard_bot, "testuser")
        assert keyboard_bot._prompt_stage(1)["stage"] == "password"
    else:
        press(keyboard_bot, "testuser")
        assert keyboard_bot._prompt_stage(1)["stage"] == "choose_preset"


def test_an_unrecognised_label_is_not_a_button(keyboard_bot):
    """Anything outside BUTTON_COMMANDS falls through to the prompt."""
    press(keyboard_bot, "🔑 /login")
    if keyboard_bot._prompt_stage(1)["stage"] == "username":
        press(keyboard_bot, "someone-typed-this")
        assert keyboard_bot._prompt_stage(1)["username"] == "someone-typed-this"


# ------------------------------------------------------ the keyboard on /start

def test_start_attaches_the_keyboard(keyboard_bot):
    upd = Upd()
    run(keyboard_bot.on_start(upd, None))
    markup = upd.message.replies[0][1]
    assert markup is not None
    labels = [b.text for row in markup.keyboard for b in row]
    assert any("/status" in l for l in labels)


def test_start_shows_inline_buttons_when_presets_exist(keyboard_bot, monkeypatch,
                                                       tmp_path):
    """The preset choice is a question, so it wants inline buttons.

    Uses chat 2: chat 1 already has credentials from the fixture, and a chat
    that is ready to submit gets the standing reply keyboard instead.
    """
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    import access as acc
    import bot as reloaded
    import ledger as led
    led.init()
    acc.grant(2, "claim")
    assert reloaded.has_credentials(2) is False
    upd = Upd(chat_id=2)
    run(reloaded.on_start(upd, None))
    markup = upd.message.replies[0][1]
    labels = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "cred:preset" in labels
    assert "cred:new" in labels