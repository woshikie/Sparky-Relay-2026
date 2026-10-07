"""The reply keyboard, and the buttons that arrive through it.

A reply keyboard is plain text over the message box, so its labels reach the bot
as ordinary messages. That is the whole reason for two decisions worth testing:
the buttons are dispatched inside `on_text` rather than as a second handler
(python-telegram-bot stops at the first match within a group), and a tapped
button is resolved before the Credentials Prompt, so "Submit steps" mid-prompt
cannot be read as a password.

The standing commands are a reply keyboard rather than inline, because inline
buttons live on one message and scroll away — which is what left the user with
no visible way to reach anything but the preset choice.
"""
import asyncio

import pytest
from conftest import run


@pytest.fixture
def keyboard_bot(bot, access, ledger):
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)
    return bot


def labels_of(markup):
    return [b.text for row in markup.keyboard for b in row]


# --------------------------------------------------------------- the layout

def test_the_keyboard_offers_the_standing_commands(keyboard_bot):
    labels = labels_of(keyboard_bot.kb_reply())
    for expected in ("Sign in", "Sign out", "Status", "History", "Help",
                     "Submit steps"):
        assert any(expected in l for l in labels), expected


def test_no_button_asks_you_to_remember_a_slash(keyboard_bot):
    """The point of the keyboard: the commands are words, not /commands."""
    labels = labels_of(keyboard_bot.kb_reply())
    assert not any(l.strip().startswith("/") for l in labels), labels


def test_every_keyboard_label_has_a_handler(keyboard_bot):
    """A label with no entry is a button that does nothing."""
    actions = keyboard_bot.button_actions()
    for label in labels_of(keyboard_bot.kb_reply()):
        assert label in actions, label
    for label in labels_of(keyboard_bot.kb_prompt()):
        assert label in actions, label
    for label in labels_of(keyboard_bot.kb_after_login()):
        assert label in actions, label


def test_every_handler_has_a_button(keyboard_bot):
    """The reverse: a command in the table no button can reach."""
    on_keyboards = set()
    for kb in (keyboard_bot.kb_reply(), keyboard_bot.kb_prompt(),
               keyboard_bot.kb_after_login(), keyboard_bot.kb_credential_choice()):
        on_keyboards.update(labels_of(kb))
    for label in keyboard_bot.button_actions():
        assert label in on_keyboards, label


def test_the_preset_button_names_the_account(keyboard_bot):
    """Which account you are about to sign in as belongs on the button."""
    labels = labels_of(keyboard_bot.kb_credential_choice())
    assert any("Use" in l and keyboard_bot.config.SITE_USERNAME in l
               for l in labels), labels


def test_the_keyboard_is_persistent(keyboard_bot):
    assert keyboard_bot.kb_reply().is_persistent is True


def test_the_keyboard_resizes_to_its_content(keyboard_bot):
    assert keyboard_bot.kb_reply().resize_keyboard is True


def test_the_keyboard_is_not_one_time(keyboard_bot):
    """Standing commands should stay put, unlike a one-off prompt."""
    assert not keyboard_bot.kb_reply().one_time_keyboard


def test_kb_for_follows_the_prompt(keyboard_bot):
    """One function decides, so no path can send a keyboard that does not fit."""
    keyboard_bot.clear_stage(1)
    assert keyboard_bot.LABEL_SUBMIT in labels_of(keyboard_bot.kb_for(1))
    keyboard_bot.set_stage(1, "choose_preset")
    assert any("Use" in l for l in labels_of(keyboard_bot.kb_for(1)))
    assert keyboard_bot.LABEL_SUBMIT not in labels_of(keyboard_bot.kb_for(1))
    keyboard_bot.set_stage(1, "username")
    assert keyboard_bot.LABEL_CANCEL in labels_of(keyboard_bot.kb_for(1))
    keyboard_bot.clear_stage(1)


# ------------------------------------------------------------- the dispatch

class Btn:
    def __init__(self, chat_id=1, text="Status"):
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

    def markup(self, i=-1):
        return self.replies[i][1]

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))
        return self

    async def delete(self):
        self.deleted = True


class Upd:
    def __init__(self, chat_id=1, text="Status"):
        self.message = Btn(chat_id, text)
        self.effective_chat = self.message.chat
        self.effective_message = self.message
        self.callback_query = None


def press(bot, text, chat_id=1):
    upd = Upd(chat_id, text)
    run(bot.on_text(upd, None))
    return upd


def test_the_status_button_answers(keyboard_bot):
    upd = press(keyboard_bot, keyboard_bot.LABEL_STATUS)
    assert "Relay status" in upd.message.said


def test_the_log_button_answers(keyboard_bot):
    assert press(keyboard_bot, keyboard_bot.LABEL_LOG).message.said


def test_the_login_button_starts_the_prompt(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    assert keyboard_bot.prompt_stage(1)["stage"] in ("username", "choose_preset")


def test_the_logout_button_forgets(keyboard_bot, ledger):
    press(keyboard_bot, keyboard_bot.LABEL_LOGOUT)
    assert ledger.credentials_stored(1) is None


def test_the_help_button_greets(keyboard_bot):
    assert "Hello" in press(keyboard_bot, keyboard_bot.LABEL_HELP).message.said


def test_the_submit_button_asks_for_a_screenshot(keyboard_bot):
    upd = press(keyboard_bot, keyboard_bot.LABEL_SUBMIT)
    assert "screenshot" in upd.message.said.lower()


def test_the_cancel_button_stops_a_prompt(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    assert keyboard_bot.prompt_stage(1) is not None
    upd = press(keyboard_bot, keyboard_bot.LABEL_CANCEL)
    assert keyboard_bot.prompt_stage(1) is None
    assert "recorded" in upd.message.said.lower()


def test_tapping_sign_in_shows_the_prompt_keyboard(keyboard_bot):
    """The keyboard has to follow the flow, not stay on the commands."""
    upd = press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    last = upd.message.markup()
    labels = labels_of(last) if last is not None else []
    assert keyboard_bot.LABEL_CANCEL in labels, labels


def test_a_button_reply_carries_a_keyboard(keyboard_bot):
    """Otherwise Telegram keeps showing whatever it last saw."""
    for label in (keyboard_bot.LABEL_STATUS, keyboard_bot.LABEL_HELP,
                  keyboard_bot.LABEL_LOGOUT):
        upd = press(keyboard_bot, label)
        assert upd.message.markup() is not None, label


# --------------------------------------------------------- the ordering rule

def test_a_button_is_not_mistaken_for_an_answer(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    stage = keyboard_bot.prompt_stage(1)["stage"]
    press(keyboard_bot, keyboard_bot.LABEL_SUBMIT)
    assert keyboard_bot.prompt_stage(1)["stage"] == stage


def test_a_button_mid_prompt_says_what_to_do(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    upd = press(keyboard_bot, keyboard_bot.LABEL_SUBMIT)
    assert "part-way" in upd.message.said or "Finish" in upd.message.said


def test_free_text_still_reaches_the_prompt(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    press(keyboard_bot, "testuser")
    stage = keyboard_bot.prompt_stage(1)["stage"]
    assert stage in ("password", "choose_preset")


def test_an_unrecognised_label_is_not_a_button(keyboard_bot):
    press(keyboard_bot, keyboard_bot.LABEL_LOGIN)
    press(keyboard_bot, "someone-typed-this")
    stage = keyboard_bot.prompt_stage(1)
    assert stage is None or stage.get("username") == "someone-typed-this" \
        or stage.get("stage") != "username"


# ------------------------------------------------------ the keyboard on /start

def test_start_attaches_a_reply_keyboard(keyboard_bot):
    upd = Upd()
    run(keyboard_bot.on_start(upd, None))
    markup = upd.message.markup()
    labels = labels_of(markup)
    assert any("Status" in l for l in labels), labels


def test_start_sends_exactly_one_message(keyboard_bot):
    upd = Upd()
    run(keyboard_bot.on_start(upd, None))
    assert len(upd.message.replies) == 1


def test_start_uses_the_reply_keyboard_for_the_preset_choice(keyboard_bot,
                                                             monkeypatch,
                                                             tmp_path):
    """The choice was inline, so the standing commands were nowhere to be seen."""
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access as acc
    import relay.telegram as reloaded
    from relay.store import db as led
    led.init()
    acc.grant(2, "claim")
    upd = Upd(chat_id=2)
    run(reloaded.on_start(upd, None))
    labels = labels_of(upd.message.markup())
    # A reply keyboard, with both the choice and the standing commands.
    assert any("Use" in l for l in labels), labels
    assert any("Status" in l for l in labels), labels


def test_start_asks_for_a_username_when_there_are_no_presets(keyboard_bot):
    """Chat 2 is granted but holds no credentials and there are no presets."""
    keyboard_bot.access.grant(2, "manual")
    assert keyboard_bot.config.has_preset_credentials() is False
    upd = Upd(chat_id=2)
    run(keyboard_bot.on_start(upd, None))
    labels = labels_of(upd.message.markup())
    assert keyboard_bot.LABEL_CANCEL in labels
    assert "username" in upd.message.said.lower()