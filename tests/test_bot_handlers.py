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
    def chat_id(self):
        """The real Message has this; save_photo() uses it for the filename."""
        return self.chat.id

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
    # Escaped: a bare `_` would open an italic Telegram never closes.
    assert "whitelist\\_claim" in upd.message.replies[0][0]


def test_start_sends_exactly_one_message(bot, granted):
    """The greeting and the Credentials Prompt used to be two replies, which
    read as the bot talking to itself.

    Chat 2 is granted but holds no credentials, so the prompt is due and has
    to ride along on the greeting.
    """
    access_module = bot.access
    access_module.grant(2, "manual")
    upd = FakeUpdate(chat_id=2)
    run(bot.on_start(upd, None))
    assert len(upd.message.replies) == 1
    said = upd.message.said
    assert "Hello" in said
    # The prompt rides along on the greeting rather than as a second reply.
    assert "username" in said.lower()


def test_start_stays_quiet_when_credentials_are_ready(bot, granted):
    """A chat that can already sign in gets the greeting and nothing else."""
    upd = FakeUpdate(chat_id=1)
    run(bot.on_start(upd, None))
    assert len(upd.message.replies) == 1
    assert "/login" not in upd.message.said


def test_status_reports_the_memory_budget(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "can launch" in upd.message.replies[0][0]


# ------------------------------------------------------------- save_photo

class FakeFile:
    def __init__(self, data):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class FakePhotoSize:
    def __init__(self, data):
        self._f = FakeFile(data)

    async def get_file(self):
        return self._f


def _jpeg(width=400, height=300):
    """A real JPEG, small enough to be a plausible screenshot."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def test_save_photo_writes_a_jpeg(bot, tmp_path, monkeypatch):
    """The screenshot is downscaled before it is handed to the Relay."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    upd = FakeUpdate()
    upd.message.photo = [FakePhotoSize(_jpeg())]
    path = run(bot.save_photo(upd, None))
    assert path and path.endswith(".jpg")
    from PIL import Image
    with Image.open(path) as im:
        assert im.size == (400, 300)


def test_save_photo_downscales_a_large_image(bot, tmp_path, monkeypatch):
    """A 4000px screenshot is shrunk to MAX_EDGE, which is what keeps the
    upload and the site's OCR fast on a 1GB host."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    upd = FakeUpdate()
    upd.message.photo = [FakePhotoSize(_jpeg(4000, 3000))]
    path = run(bot.save_photo(upd, None))
    from PIL import Image
    with Image.open(path) as im:
        assert max(im.size) == bot.MAX_EDGE


def test_save_photo_accepts_a_document(bot, tmp_path, monkeypatch):
    """A screenshot sent as a file rather than a photo is still an image."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    upd = FakeUpdate()
    upd.message.photo = []
    upd.message.document = FakePhotoSize(_jpeg())
    upd.message.document.mime_type = "image/jpeg"
    path = run(bot.save_photo(upd, None))
    assert path and path.endswith(".jpg")


def test_save_photo_refuses_a_non_image_document(bot, tmp_path, monkeypatch):
    """A PDF is not a screenshot, and must not be saved as one."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    upd = FakeUpdate()
    upd.message.photo = []
    upd.message.document = FakePhotoSize(b"%PDF-1.4 not an image")
    upd.message.document.mime_type = "application/pdf"
    assert run(bot.save_photo(upd, None)) is None


def test_save_photo_with_nothing_to_save_returns_none(bot):
    upd = FakeUpdate()
    upd.message.photo = []
    upd.message.document = None
    assert run(bot.save_photo(upd, None)) is None


def test_save_photo_names_the_saved_file(bot, tmp_path, monkeypatch):
    """The log line is how you find out what was actually written."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    upd = FakeUpdate(chat_id=7, message_id=42)
    upd.message.photo = [FakePhotoSize(_jpeg())]
    path = run(bot.save_photo(upd, None))
    assert "7-42.jpg" in path


def test_status_reports_stored_credentials_as_encrypted(bot, granted):
    """The username is fine to show; the fact that it is stored is the point."""
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "stored, encrypted" in upd.message.replies[0][0]


def test_status_without_credentials_says_to_sign_in(bot, access, ledger):
    access.grant(5, "manual")
    upd = FakeUpdate(chat_id=5)
    run(bot.on_status(upd, None))
    assert "send /login" in upd.message.replies[0][0]


def test_status_counts_the_recorded_submissions(bot, granted, ledger):
    ledger.record("2026-10-03", 2831, "2,831", "October 3rd, 2026", 52)
    ledger.record("2026-10-04", 6532, "6,532", "October 4th, 2026", 58)
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Submissions recorded:** 2" in upd.message.replies[0][0]


def test_status_lists_the_chats_with_access(bot, granted, ledger):
    ledger.grant(9, "manual")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "9" in upd.message.replies[0][0]


def test_status_lists_the_chats_denied(bot, granted, ledger):
    ledger.deny(11, "spam")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "11" in upd.message.replies[0][0]


def test_status_says_when_nothing_is_denied(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Chats denied:** none" in upd.message.replies[0][0]


def test_status_counts_pending_confirmations(bot, granted):
    bot.PENDING[(1, 100)] = {"path": "/tmp/x.jpg", "steps": 100,
                              "reported": "100", "scratch": None,
                              "date": None}
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Pending confirmations:** 1" in upd.message.replies[0][0]
    bot.PENDING.clear()


def test_status_says_when_nothing_is_pending(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Pending confirmations:** 0" in upd.message.replies[0][0]


def test_status_reports_the_browser_configuration(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "headless=" in upd.message.replies[0][0]
    assert "per Screenshot" in upd.message.replies[0][0]


def test_status_says_no_when_the_browser_cannot_launch(bot, granted, monkeypatch):
    """The one word that tells the user not to bother sending a screenshot."""
    monkeypatch.setattr(bot.memory, "budget", lambda: {
        "total_mb": 950.0, "available_mb": 10.0,
        "browser_peak_mb": 780, "min_free_mb": 780, "can_launch": False,
        "cgroup": True, "cgroup_limit_mb": 950.0,
        "cgroup_current_mb": 850.0, "cgroup_committed_mb": 850.0})
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "can launch: **NO**" in upd.message.replies[0][0]


def test_status_refreshes_the_keyboard(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert upd.message.replies[0][1] is not None
