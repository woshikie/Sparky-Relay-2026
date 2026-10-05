"""The handler layer in bot.py: photos, callbacks, and the confirmation flow.

python-telegram-bot handlers are async, so these drive them with a small fake
Update/message pair rather than a live connection. The goal is to cover the
decision logic — the guard against submitting something the user did not
approve — not Telegram's own plumbing.
"""
import asyncio
import datetime

import pytest


# --------------------------------------------------------------- fakes

class FakeMessage:
    def __init__(self, chat_id=1, message_id=100, text=None):
        self.chat = FakeChat(chat_id)
        self.message_id = message_id
        self.text = text
        self.replies = []
        self.edits = []
        self.deleted = False
        self.markup = None
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
    def effective_user(self):
        return self.chat

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))
        return FakeMessage(self.chat.id, self.message_id + len(self.replies))

    @property
    def said(self):
        """Everything this message said, joined. Handy for `not in` assertions."""
        return " ".join(t for t, _ in self.replies)

    async def reply_photo(self, *a, **kw):
        return FakeMessage(self.chat.id, self.message_id + 99)

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)
        self.markup = kw.get("reply_markup")
        return self

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markup = reply_markup
        return self

    async def delete(self):
        self.deleted = True
        return True


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.username = "testuser"
        self.first_name = "Test"


class FakeUpdate:
    def __init__(self, chat_id=1, message_id=100, text=None):
        self.message = FakeMessage(chat_id, message_id, text)
        self.effective_chat = self.message.chat
        self.effective_message = self.message
        self.callback_query = None


class FakeQuery:
    def __init__(self, data, chat_id=1, message_id=200):
        self.data = data
        self.answers = []
        self.message = FakeMessage(chat_id, message_id)
        self.effective_chat = self.message.chat
        self.effective_message = self.message

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.message.edits.append(text)
        self.message.markup = kw.get("reply_markup")
        return self.message

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.message.markup = reply_markup
        return self.message


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def granted(bot, access):
    """A chat that has been granted access and holds credentials."""
    access.grant(1, "claim")
    import ledger
    ledger.save_credentials(1, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)
    return 1


# ------------------------------------------------------------ access gates

def test_a_denied_chat_cannot_send_a_photo(bot, access, ledger):
    access.deny(5, "spam")
    upd = FakeUpdate(chat_id=5)
    run(bot.on_photo(upd, None))
    text = upd.message.replies[0][0]
    assert "deny list" in text
    assert ledger.credentials_stored(5) is None


def test_a_photo_before_credentials_asks_for_them(bot, access):
    """A photo with no credentials must not look like it was read.

    The site does the OCR inside the page, so without credentials the browser
    cannot be signed in and no number can exist. The reply has to ask, not
    quietly fail.
    """
    access.grant(2, "claim")
    upd = FakeUpdate(chat_id=2)
    run(bot.on_photo(upd, None))
    assert "Detected steps" not in upd.message.said
    assert upd.message.replies, "the user is told something happened"


def test_status_is_refused_to_a_stranger(bot, access):
    """Whatever the mode, an unauthorised chat gets a reason, not data."""
    upd = FakeUpdate(chat_id=3)
    run(bot.on_status(upd, None))
    text = upd.message.replies[0][0]
    assert "Relay" in text or "Shared Secret" in text
    assert "Submissions recorded" not in text


def test_log_is_refused_to_a_stranger(bot, access):
    upd = FakeUpdate(chat_id=4)
    run(bot.on_log(upd, None))
    assert upd.message.replies, "a refusal is still a reply"


def test_login_is_refused_to_a_stranger(bot, access):
    upd = FakeUpdate(chat_id=6)
    run(bot.on_login(upd, None))
    assert bot._prompt_stage(6) is None


def test_logout_is_refused_to_a_stranger(bot, access, ledger):
    ledger.save_credentials(6, "testuser", "p", bot.config.TELEGRAM_BOT_TOKEN)
    upd = FakeUpdate(chat_id=6)
    run(bot.on_logout(upd, None))
    # Still stored: the stranger did not get to erase anything either way,
    # but they also learned nothing.
    assert ledger.credentials_stored(6) is not None


# ------------------------------------------------------------- /start paths

def test_start_claims_for_the_first_chat(bot, access):
    upd = FakeUpdate(chat_id=11)
    run(bot.on_start(upd, None))
    assert "owns the Relay" in upd.message.replies[0][0]
    assert access.check(11)


def test_start_refuses_a_second_claim(bot, access):
    run(bot.on_start(FakeUpdate(chat_id=11), None))
    upd = FakeUpdate(chat_id=12)
    run(bot.on_start(upd, None))
    assert "already" in upd.message.replies[0][0]
    assert not access.check(12)


def test_start_in_shared_secret_mode_prompts(bot, monkeypatch, tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    upd = FakeUpdate(chat_id=13)
    run(reloaded.on_start(upd, None))
    assert "Shared Secret" in upd.message.replies[0][0]


# --------------------------------------------------------- the prompt

def test_login_starts_the_username_stage(bot, access):
    access.grant(1, "claim")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_login(upd, None))
    assert bot._prompt_stage(1)["stage"] == "username"


def test_a_username_advances_to_the_password(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="testuser"), None))
    assert bot._prompt_stage(1)["stage"] == "password"
    assert bot._prompt_stage(1)["username"] == "testuser"


def test_a_short_username_is_refused(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="ab"), None))
    assert bot._prompt_stage(1)["stage"] == "username"   # still asking


def test_a_long_username_is_refused(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="x" * 60), None))
    assert bot._prompt_stage(1)["stage"] == "username"


def test_a_password_is_stored_and_the_message_deleted(bot, access, ledger):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="testuser"), None))
    upd = FakeUpdate(chat_id=1, text="testpass123")
    run(bot.on_text(upd, None))
    assert ledger.load_credentials(1, bot.config.TELEGRAM_BOT_TOKEN) == \
        ("testuser", "testpass123")
    assert upd.message.deleted, "the message carrying the password is removed"
    assert bot._prompt_stage(1)["stage"] == "ready"


def test_the_password_is_not_echoed(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="testuser"), None))
    upd = FakeUpdate(chat_id=1, text="testpass123")
    run(bot.on_text(upd, None))
    assert "testpass123" not in upd.message.said


def test_logout_forgets_the_credentials(bot, access, ledger):
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)
    run(bot.on_logout(FakeUpdate(chat_id=1), None))
    assert ledger.credentials_stored(1) is None


def test_an_unprompted_message_is_answered_not_swallowed(bot, access):
    access.grant(1, "claim")
    upd = FakeUpdate(chat_id=1, text="hello?")
    run(bot.on_text(upd, None))
    assert "/login" in upd.message.replies[0][0]


# --------------------------------------------------- shared secret exchange

def test_a_correct_secret_is_accepted_and_the_message_deleted(bot, monkeypatch,
                                                             tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    ledger.init_secrets_from_env("testuser:s3cret-passphrase")
    upd = FakeUpdate(chat_id=20, text="s3cret-passphrase")
    run(reloaded.on_text(upd, None))
    assert access.check(20)
    assert upd.message.deleted
    assert "accepted" in upd.message.replies[0][0]


def test_a_wrong_secret_is_rejected_and_the_message_deleted(bot, monkeypatch,
                                                           tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    ledger.init_secrets_from_env("testuser:s3cret-passphrase")
    upd = FakeUpdate(chat_id=21, text="guess")
    run(reloaded.on_text(upd, None))
    assert not access.check(21)
    assert upd.message.deleted


def test_a_throttled_chat_is_told_to_wait(bot, monkeypatch, tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    ledger.init_secrets_from_env("testuser:s3cret-passphrase")
    run(reloaded.on_text(FakeUpdate(chat_id=22, text="guess"), None))
    upd = FakeUpdate(chat_id=22, text="s3cret-passphrase")
    run(reloaded.on_text(upd, None))
    assert "not a lockout" in upd.message.said


def test_a_non_secret_never_reaches_the_prompt(bot, access):
    """An unauthorised chat's chatty text is not an answer to anything."""
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=99, text="ignore me"), None))
    assert bot._prompt_stage(99) is None
    assert bot._prompt_stage(1)["stage"] == "username"


# ---------------------------------------------------------- preset choice

def test_the_preset_choice_is_offered_when_configured(bot, monkeypatch, tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    access.grant(1, "claim")
    upd = FakeUpdate(chat_id=1)
    run(reloaded.on_start(upd, None))
    assert "preset credentials" in " ".join(t for t, _ in upd.message.replies)
    assert reloaded._prompt_stage(1)["stage"] == "choose_preset"


def test_choosing_the_preset_uses_it(bot, monkeypatch, tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    access.grant(1, "claim")
    reloaded._set_stage(1, "choose_preset")
    q = FakeQuery("cred:preset", chat_id=1)
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = q
    run(reloaded.on_credential_choice(upd, None))
    assert reloaded.site_credentials(1) == ("testuser", "pw")


def test_choosing_new_moves_to_the_username(bot, monkeypatch, tmp_path):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    import bot as reloaded
    import ledger
    ledger.init()
    access.grant(1, "claim")
    reloaded._set_stage(1, "choose_preset")
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = FakeQuery("cred:new", chat_id=1)
    run(reloaded.on_credential_choice(upd, None))
    assert reloaded._prompt_stage(1)["stage"] == "username"


def test_a_stale_preset_button_is_refused(bot):
    q = FakeQuery("cred:preset", chat_id=1)
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = q
    run(bot.on_credential_choice(upd, None))
    assert "Nothing to choose" in q.answers[0][0]


# ------------------------------------------------------------------ /status

def test_status_never_shows_a_password(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    text = upd.message.replies[0][0]
    assert "testpass123" not in text
    assert "testuser" in text


def test_status_reports_the_access_mode(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "whitelist_claim" in upd.message.replies[0][0]


def test_status_reports_the_memory_budget(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "can launch" in upd.message.replies[0][0]
