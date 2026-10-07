"""The handler layer in bot.py: photos, callbacks, and the confirmation flow.

python-telegram-bot handlers are async, so these drive them with a small fake
Update/message pair rather than a live connection. The goal is to cover the
decision logic — the guard against submitting something the user did not
approve — not Telegram's own plumbing.
"""
import asyncio
import contextlib
import datetime
import sys

from telegram.error import TelegramError

import pytest
from conftest import run


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


@pytest.fixture
def granted(bot, access):
    """A chat that has been granted access and holds credentials."""
    access.grant(1, "claim")
    from relay.store import ledger
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
    assert bot.prompt_stage(6) is None


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


def test_start_in_shared_secret_mode_prompts(bot, monkeypatch, tmp_path, db):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db
    db.init()
    upd = FakeUpdate(chat_id=13)
    run(reloaded.on_start(upd, None))
    assert "Shared Secret" in upd.message.replies[0][0]


# --------------------------------------------------------- the prompt

def test_login_starts_the_username_stage(bot, access):
    access.grant(1, "claim")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_login(upd, None))
    assert bot.prompt_stage(1)["stage"] == "username"


def test_a_username_advances_to_the_password(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="testuser"), None))
    assert bot.prompt_stage(1)["stage"] == "password"
    assert bot.prompt_stage(1)["username"] == "testuser"


def test_a_short_username_is_refused(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="ab"), None))
    assert bot.prompt_stage(1)["stage"] == "username"   # still asking


def test_a_long_username_is_refused(bot, access):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="x" * 60), None))
    assert bot.prompt_stage(1)["stage"] == "username"


def test_a_password_is_stored_and_the_message_deleted(bot, access, ledger):
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=1, text="testuser"), None))
    upd = FakeUpdate(chat_id=1, text="testpass123")
    run(bot.on_text(upd, None))
    assert ledger.load_credentials(1, bot.config.TELEGRAM_BOT_TOKEN) == \
        ("testuser", "testpass123")
    assert upd.message.deleted, "the message carrying the password is removed"
    assert bot.prompt_stage(1)["stage"] == "ready"


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

def test_a_correct_secret_is_accepted_and_the_message_deleted(bot, monkeypatch, tmp_path, policy, db):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db, policy
    db.init()
    policy.init_secrets_from_env("testuser:s3cret-passphrase")
    upd = FakeUpdate(chat_id=20, text="s3cret-passphrase")
    run(reloaded.on_text(upd, None))
    assert access.check(20)
    assert upd.message.deleted
    assert "accepted" in upd.message.replies[0][0]


def test_a_wrong_secret_is_rejected_and_the_message_deleted(bot, monkeypatch, tmp_path, policy, db):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db, policy
    db.init()
    policy.init_secrets_from_env("testuser:s3cret-passphrase")
    upd = FakeUpdate(chat_id=21, text="guess")
    run(reloaded.on_text(upd, None))
    assert not access.check(21)
    assert upd.message.deleted


def test_a_throttled_chat_is_told_to_wait(bot, monkeypatch, tmp_path, policy, db):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db, policy
    db.init()
    policy.init_secrets_from_env("testuser:s3cret-passphrase")
    run(reloaded.on_text(FakeUpdate(chat_id=22, text="guess"), None))
    upd = FakeUpdate(chat_id=22, text="s3cret-passphrase")
    run(reloaded.on_text(upd, None))
    assert "not a lockout" in upd.message.said


def test_a_non_secret_never_reaches_the_prompt(bot, access):
    """An unauthorised chat's chatty text is not an answer to anything."""
    access.grant(1, "claim")
    run(bot.on_login(FakeUpdate(chat_id=1), None))
    run(bot.on_text(FakeUpdate(chat_id=99, text="ignore me"), None))
    assert bot.prompt_stage(99) is None
    assert bot.prompt_stage(1)["stage"] == "username"


# ---------------------------------------------------------- preset choice

def test_the_preset_choice_is_offered_when_configured(bot, monkeypatch, tmp_path, db):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db
    db.init()
    access.grant(1, "claim")
    upd = FakeUpdate(chat_id=1)
    run(reloaded.on_start(upd, None))
    assert "preset credentials" in " ".join(t for t, _ in upd.message.replies)
    assert reloaded.prompt_stage(1)["stage"] == "choose_preset"


def test_choosing_the_preset_uses_it(bot, monkeypatch, tmp_path, db):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db
    db.init()
    access.grant(1, "claim")
    reloaded.set_stage(1, "choose_preset")
    q = FakeQuery("cred:preset", chat_id=1)
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = q
    run(reloaded.on_credential_choice(upd, None))
    assert reloaded.site_credentials(1) == ("testuser", "pw")


def test_choosing_new_moves_to_the_username(bot, monkeypatch, tmp_path, db):
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access
    import relay.telegram as reloaded
    from relay.store import db
    db.init()
    access.grant(1, "claim")
    reloaded.set_stage(1, "choose_preset")
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = FakeQuery("cred:new", chat_id=1)
    run(reloaded.on_credential_choice(upd, None))
    assert reloaded.prompt_stage(1)["stage"] == "username"


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


def test_status_lists_the_chats_with_access(bot, granted, ledger, policy):
    policy.grant(9, "manual")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "9" in upd.message.replies[0][0]


def test_status_lists_the_chats_denied(bot, granted, ledger, policy):
    policy.deny(11, "spam")
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "11" in upd.message.replies[0][0]


def test_status_says_when_nothing_is_denied(bot, granted):
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Chats denied:** none" in upd.message.replies[0][0]


def test_status_counts_pending_confirmations(bot, granted):
    bot.pending.put((1, 100), {"path": "/tmp/x.jpg", "steps": 100,
                              "reported": "100", "scratch": None,
                              "date": None})
    upd = FakeUpdate(chat_id=1)
    run(bot.on_status(upd, None))
    assert "Pending confirmations:** 1" in upd.message.replies[0][0]
    bot.pending.clear()


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


# ------------------------------------------------------------- on_photo

def _session_without_browser(session, monkeypatch):
    """Real sign_in, no browser: the session yields only after resolving creds.

    Two photo tests need the NoCredentials failure, which lives in the real
    sign_in -- but entering a real session launches real Firefox. This double
    keeps the credential resolution and skips the launch, so the suite runs
    anywhere, including CI runners with no browser at all.
    """

    @contextlib.asynccontextmanager
    async def fake(chat_id=None, progress=None):
        await session.sign_in(chat_id, progress)
        yield None
        raise AssertionError("entered a session that should have failed sign-in")

    monkeypatch.setattr(session, "browser_session", fake)


def test_a_photo_before_credentials_asks_for_them(bot, access, ledger,
                                                        monkeypatch, session):
    """The existing test asserts the reply; this one asserts the prompt."""
    _session_without_browser(session, monkeypatch)
    access.grant(3, "manual")
    upd = FakeUpdate(chat_id=3)
    upd.message.photo = [FakePhotoSize(_jpeg())]
    run(bot.on_photo(upd, None))
    assert "username" in upd.message.said.lower()


def test_a_photo_from_an_unclaimed_chat_is_refused(bot, access):
    """In whitelist_claim mode, a chat that has not claimed is refused.

    The reply has to say why, or the user has no way to know whether to claim
    or to switch Access Mode.
    """
    upd = FakeUpdate(chat_id=4)
    upd.message.photo = [FakePhotoSize(_jpeg())]
    run(bot.on_photo(upd, None))
    assert "already been claimed" in upd.message.said
    assert "whitelist" in upd.message.said


def test_a_photo_with_no_credentials_and_no_presets_offers_them(
        bot, access, monkeypatch, tmp_path, session):
    """No stored credentials and nothing preset: the prompt is due."""
    _session_without_browser(session, monkeypatch)
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    access.grant(8, "manual")
    upd = FakeUpdate(chat_id=8)
    upd.message.photo = [FakePhotoSize(_jpeg())]
    run(bot.on_photo(upd, None))
    assert "username" in upd.message.said.lower()


# ------------------------------------------------------------------ /sync

def test_sync_is_refused_to_a_stranger(bot, access):
    upd = FakeUpdate(chat_id=20)
    run(bot.on_sync(upd, None))
    assert "already been claimed" in upd.message.said


def test_sync_before_credentials_offers_them(bot, access):
    access.grant(21, "manual")
    upd = FakeUpdate(chat_id=21)
    run(bot.on_sync(upd, None))
    assert "username" in upd.message.said.lower()


def test_sync_completes_when_the_site_has_nothing(bot, access, ledger, monkeypatch, session):
    """An empty sync runs the whole path and stores nothing.

    The final text goes to the scratch message, which reply_text() returns as
    a separate object, so what is asserted here is that the sync ran: the
    syncing announcement was sent, and nothing was written to the ledger.
    """
    access.grant(22, "manual")
    ledger.save_credentials(22, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)

    class EmptyRelay:
        def read_days(self):
            return []

    @contextlib.asynccontextmanager
    async def fake_session(chat_id=None, progress=None):
        yield EmptyRelay()
    monkeypatch.setattr(session, "browser_session", fake_session)

    upd = FakeUpdate(chat_id=22)
    run(bot.on_sync(upd, None))
    assert "submits nothing" in upd.message.said
    assert ledger.all_site_days() == []


# -------------------------------------------- the remaining on_text branches

def test_an_empty_message_when_a_secret_is_needed_is_ignored(bot, monkeypatch, tmp_path):
    """A blank message must not be treated as a secret attempt."""
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                RELAY_STATE_DIR=str(tmp_path))
    import relay.telegram as reloaded
    upd = FakeUpdate(chat_id=30, text="")
    run(reloaded.on_text(upd, None))
    assert upd.message.replies == []


def test_a_throttled_chat_is_told_why_the_secret_was_deleted(bot, monkeypatch, tmp_path, policy, db):
    """The message carrying a secret is scrubbed even when it did not work.

    Falling through to the 'nothing to do' reply would leave a password
    sitting in the chat with no explanation.
    """
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                SHARED_SECRETS="testuser:s3cret-passphrase",
                RELAY_STATE_DIR=str(tmp_path))
    import relay.telegram as reloaded
    from relay.store import db, policy
    db.init()
    policy.init_secrets_from_env("testuser:s3cret-passphrase")
    run(reloaded.on_text(FakeUpdate(chat_id=31, text="guess"), None))
    upd = FakeUpdate(chat_id=31, text="s3cret-passphrase")
    run(reloaded.on_text(upd, None))
    assert upd.message.deleted
    assert "not a lockout" in upd.message.said


def test_a_password_that_fails_to_store_says_so(bot, access, monkeypatch):
    """A vault failure must not look like a saved password."""
    access.grant(32, "manual")
    bot.set_stage(32, "password", username="testuser")

    def boom(*a, **kw):
        raise vault.DecryptionFailed("the key no longer fits")
    monkeypatch.setattr(bot.ledger, "save_credentials", boom)

    upd = FakeUpdate(chat_id=32, text="testpass123")
    run(bot.on_text(upd, None))
    assert "could not" in upd.message.said.lower() or "failed" in upd.message.said.lower()


def test_a_password_that_fails_to_store_still_clears_the_stage(bot, access, monkeypatch):
    """The stage is cleared in a finally, so a failure cannot wedge the prompt."""
    access.grant(33, "manual")
    bot.set_stage(33, "password", username="testuser")

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(bot.ledger, "save_credentials", boom)

    upd = FakeUpdate(chat_id=33, text="testpass123")
    run(bot.on_text(upd, None))
    assert bot.prompt_stage(33) is None


# ------------------------------------------------------------- scrub

def test_a_scrub_that_cannot_delete_says_so(bot):
    """A bot can delete in a private chat; when it cannot, say so rather than
    leaving a password in the chat with no explanation."""
    from telegram.error import TelegramError

    class Undeletable(FakeMessage):
        async def delete(self):
            raise TelegramError("message can't be deleted")

        async def reply_text(self, text, **kw):
            self.replies.append((text, None))
            return self

    msg = Undeletable()
    run(bot.scrub(msg))
    assert "could not" in msg.said.lower() or "delete" in msg.said.lower()


def test_a_scrub_that_cannot_delete_or_reply_is_silent(bot):
    """Both failing must not raise: the secret is already in the chat."""
    from telegram.error import TelegramError

    class Hopeless(FakeMessage):
        async def delete(self):
            raise TelegramError("nope")

        async def reply_text(self, text, **kw):
            raise TelegramError("nope")

    run(bot.scrub(Hopeless()))


def test_a_scrub_that_deletes_cleanly_says_nothing(bot):
    msg = FakeMessage()
    run(bot.scrub(msg))
    assert msg.deleted
    assert msg.replies == []


# ------------------------------------------------------ site_credentials

def test_a_preset_that_is_no_longer_configured_is_refused(bot, monkeypatch):
    """The stage says preset, but the config no longer has one.

    This is the rotated-config case: the chat was set up when a preset existed,
    and the operator has since removed it. The answer must be the specific
    'preset is gone' error, not a generic 'no credentials'.
    """
    from relay.store import vault
    bot.set_stage(1, "username", preset=True)
    assert bot.config.has_preset_credentials() is False
    with pytest.raises(vault.DecryptionFailed):
        bot.site_credentials(1)
    bot.clear_stage(1)


# ------------------------------------------------------------ backup_job

def test_the_backup_is_delivered_to_telegram_itself(bot, ledger, tmp_path):
    """The nightly backup goes to Telegram, so it survives the host."""
    bot.policy.remember_user(1, "testuser")
    bot.ledger.record("2026-10-03", 2831, "2,831", "October 3rd, 2026", 52)

    sent = []

    class FakeBot:
        async def send_document(self, **kw):
            sent.append(kw)

    class FakeCtx:
        bot = FakeBot()

    run(bot.backup_job(FakeCtx()))
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 1
    assert "relay-ledger" in sent[0]["filename"]
    assert "1 Submission" in sent[0]["caption"]


def test_the_backup_with_no_users_does_nothing(bot):
    """No users means no delivery, not an error."""
    run(bot.backup_job(type("C", (), {"bot": type("B", (), {})()})()))


# ------------------------------------------------------------------ main()

def test_main_exits_cleanly_when_the_token_is_missing(bot, monkeypatch, capsys):
    """A missing token is the one config error that reaches main().

    ACCESS_MODE is validated at import, so this is the branch that actually
    runs: a plain message on stderr and a non-zero exit, not a traceback.

    The exit is SystemExit carrying the message, which Python prints to stderr
    and exits 1 -- so the code is the message, not 2.
    """
    monkeypatch.setattr(bot.config, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit) as exc:
        bot.main()
    assert "missing configuration" in str(exc.value)
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_main_installs_the_timestamps(bot, monkeypatch):
    """console.install() runs first, so even the refusal is timestamped."""
    monkeypatch.setattr(bot.config, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit):
        bot.main()
    from relay import console as console_mod
    assert isinstance(sys.stdout, console_mod.TimestampedStream)
    console_mod.uninstall()


# ------------------------------------------- on_photo's failure branches

def _photo_with_failing_session(bot, access, ledger, exc, tmp_path, monkeypatch,
                                session):
    """A photo where the browser session raises `exc`."""
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    access.grant(40, "manual")
    ledger.save_credentials(40, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)

    @contextlib.asynccontextmanager
    async def failing(chat_id=None, progress=None):
        raise exc
        yield

    monkeypatch.setattr(session, "browser_session", failing)
    upd = FakeUpdate(chat_id=40)
    upd.message.photo = [FakePhotoSize(_jpeg())]
    return upd


def test_a_photo_with_no_credentials_says_so(bot, access, ledger, tmp_path, monkeypatch, session):
    upd = _photo_with_failing_session(bot, access, ledger, bot.NoCredentials(),
                                      tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "username" in upd.message.said.lower()


def test_a_photo_with_a_rotated_token_says_the_password_is_gone(bot, access, ledger, tmp_path, monkeypatch, session):
    """A rotated bot token means the vault key no longer fits. The old password
    is unrecoverable by design, and the reply has to say so."""
    from relay.store import vault
    upd = _photo_with_failing_session(
        bot, access, ledger, vault.DecryptionFailed("the key no longer fits"),
        tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "gone" in upd.message.said.lower() or "again" in upd.message.said.lower()


def test_a_photo_with_too_little_memory_says_so(bot, access, ledger, tmp_path, monkeypatch, session):
    upd = _photo_with_failing_session(
        bot, access, ledger,
        bot.memory.InsufficientMemory({"available_mb": 10, "min_free_mb": 780}),
        tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "memory" in upd.message.said.lower()


def test_a_photo_where_the_site_read_nothing_says_so(bot, access, ledger, tmp_path, monkeypatch, session):
    upd = _photo_with_failing_session(
        bot, access, ledger, bot.relay_site.NoStepsFound(),
        tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "could not read" in upd.message.said.lower()


def test_a_photo_where_the_site_changed_says_so(bot, access, ledger, tmp_path, monkeypatch, session):
    upd = _photo_with_failing_session(
        bot, access, ledger,
        bot.relay_site.SiteChanged("the upload form is gone"),
        tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "changed" in upd.message.said.lower()


def test_a_photo_that_fails_for_any_other_reason_says_so(bot, access, ledger, tmp_path, monkeypatch, session):
    """The catch-all: a submission must never fail because of a progress line."""
    upd = _photo_with_failing_session(
        bot, access, ledger, RuntimeError("something unexpected"),
        tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "failed" in upd.message.said.lower()


def test_a_photo_failure_reports_on_the_original_message(bot, access, ledger, tmp_path, monkeypatch, session):
    """The failure reply goes to the message the user sent, not the checklist.

    The scratch is a separate object that reply_text() returns, so the
    'Upload failed' reply has to be addressed to the original message or the
    user would never see it.
    """
    upd = _photo_with_failing_session(
        bot, access, ledger, RuntimeError("boom"), tmp_path, monkeypatch, session)
    run(bot.on_photo(upd, None))
    assert "Upload failed" in upd.message.said


# ------------------------------------------------------------------- log()

def test_log_is_quiet_without_a_context(bot, capsys):
    """No context means no job_queue to touch, and the line still prints."""
    bot.log(None, "hello")
    assert "hello" in capsys.readouterr().out


def test_log_with_a_context_prints(bot, capsys):
    bot.log(type("C", (), {"job_queue": None})(), "hello")
    assert "hello" in capsys.readouterr().out


def test_log_survives_a_job_queue_that_raises(bot, capsys):
    """A broken job_queue must not take down the log line."""
    class BadQ:
        def run_once(self, *a, **kw):
            raise RuntimeError("no scheduler")
    bot.log(type("C", (), {"job_queue": BadQ()})(), "hello")
    assert "hello" in capsys.readouterr().out


def test_the_old_site_login_entry_point_is_gone(bot):
    """It was superseded by browser_session(); calling it must say so."""
    with pytest.raises(RuntimeError):
        run(bot.site_login(None))


# ------------------------------------------------------- the prompt helpers

def test_ask_credentials_with_no_presets_asks_for_a_username(bot, access):
    access.grant(50, "manual")
    msg = FakeMessage(chat_id=50)
    run(bot.ask_credentials(msg, 50))
    assert bot.prompt_stage(50)["stage"] == "username"
    assert "username" in msg.said.lower()


def test_ask_username_sets_the_stage(bot, access):
    access.grant(51, "manual")
    msg = FakeMessage(chat_id=51)
    run(bot.ask_username(msg, 51))
    assert bot.prompt_stage(51)["stage"] == "username"


def test_ask_password_carries_the_username_forward(bot, access):
    """The username has to survive the transition, or the password is orphaned."""
    access.grant(52, "manual")
    msg = FakeMessage(chat_id=52)
    run(bot.ask_password(msg, 52, username="testuser"))
    st = bot.prompt_stage(52)
    assert st["stage"] == "password"
    assert st["username"] == "testuser"


def test_ask_password_marks_a_preset(bot, access):
    access.grant(53, "manual")
    msg = FakeMessage(chat_id=53)
    run(bot.ask_password(msg, 53, username="testuser", preset=True))
    assert bot.prompt_stage(53)["preset"] is True


def test_kb_done_returns_the_standing_commands(bot):
    labels = [b.text for row in bot.kb_done().keyboard for b in row]
    assert any("Status" in l for l in labels)


# ------------------------------------------------------------ on_ready

def test_on_ready_announces_the_bot(bot, capsys):
    """The first line in the log, and the only one before polling starts."""
    class FakeBot:
        async def get_me(self):
            return type("M", (), {"username": "sparky_2026_bot"})()
    run(bot.on_ready(type("A", (), {"bot": FakeBot()})()))
    assert "sparky_2026_bot" in capsys.readouterr().out


# ----------------------------------------------------------- parse_profile

def test_the_profile_parser_finds_both_totals(bot):
    """Parsing returns numbers, not copy: formatting is the caller's job."""
    assert bot.parse_profile("24,860 total steps\n248 total points") == {
        "total_steps": 24860, "total_points": 248, "house": None}


def test_the_profile_parser_finds_only_steps(bot):
    assert bot.parse_profile("24,860 total steps") == {
        "total_steps": 24860, "total_points": None, "house": None}


def test_the_profile_parser_finds_the_house(bot):
    assert bot.parse_profile("YOUR HOUSE\n\nGryffindor") == {
        "total_steps": None, "total_points": None, "house": "Gryffindor"}


def test_the_profile_parser_on_a_board_with_nothing(bot):
    """A board with no totals must not raise, and must not invent numbers."""
    assert bot.parse_profile("nothing here") is None


def test_a_profile_formats_into_the_rank_line(bot):
    """The rank line after Commit: numbers in, copy out."""
    profile = bot.parse_profile("24,860 total steps\n248 total points")
    line = bot.words.rank_line(profile["total_steps"], profile["total_points"],
                               profile["house"])
    assert "24,860" in line
    assert "248" in line


# --------------------------------------------------- the access-refused paths

def test_login_is_refused_with_a_reason(bot, access):
    """The refusal says why, so the user knows whether to claim or switch."""
    upd = FakeUpdate(chat_id=60)
    run(bot.on_login(upd, None))
    assert "already been claimed" in upd.message.said
    assert "whitelist" in upd.message.said


def test_logout_is_refused_to_a_stranger(bot, access):
    upd = FakeUpdate(chat_id=61)
    run(bot.on_logout(upd, None))
    assert "cannot drive" in upd.message.said or "already been claimed" in upd.message.said


# ------------------------------------------------------- cb_date's guard

def test_a_date_callback_that_raises_is_answered(bot, access):
    """A callback that raises must not leave the button spinning."""
    access.grant(70, "manual")
    q = FakeQuery("dt:today")
    q.message.reply_to_message = None
    # Force _cb_date to raise by giving it a message with no reply_to_message
    # and no pending -- the expired path. That must be answered, not hang.
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_date(upd, None))
    assert q.answers, "the callback was never answered"


def test_a_date_callback_that_cannot_even_be_answered_is_survived(bot, access):
    """If answering itself raises, the handler must not propagate.

    The outer guard answers the query so the client stops spinning; if that
    answer fails too, the exception must be swallowed rather than propagated.
    """
    access.grant(71, "manual")

    class SilentQuery(FakeQuery):
        async def answer(self, text=None, show_alert=False):
            raise TelegramError("nope")

    q = SilentQuery("dt:today")
    q.message.reply_to_message = None
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_date(upd, None))


def test_an_unauthorised_date_callback_is_refused(bot, access):
    access.deny(72, "spam")
    q = FakeQuery("dt:today", chat_id=72)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_date(upd, None))
    assert q.answers and q.answers[0][1], "an alert, not a silent change"


# ------------------------------------------------------- cb_ok's early paths

def test_a_confirm_from_an_unauthorised_chat_is_refused(bot, access):
    access.deny(73, "spam")
    q = FakeQuery("ok:go", chat_id=73)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_ok(upd, None))
    assert q.answers and q.answers[0][1]


def test_the_recording_line_names_the_number_and_date(bot, access, monkeypatch, session):
    """The intermediate edit: the user sees it is working on their number."""
    access.grant(74, "manual")
    bot.pending.put((74, 100), {
        "path": "/tmp/x.jpg", "steps": 6532, "reported": "6,532",
        "scratch": FakeMessage(), "date": datetime.date(2026, 10, 4),
        "iso": "2026-10-04", "label": "October 4th, 2026"})

    class R:
        def upload(self, path, mode="steps"):
            return 6532, "6,532"
        def set_date(self, d):
            pass
        def commit(self, steps):
            return "Recorded"

    @contextlib.asynccontextmanager
    async def fake_session(chat_id=None, progress=None):
        yield R()
    monkeypatch.setattr(session, "browser_session", fake_session)

    q = FakeQuery("ok:go", chat_id=74)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_ok(upd, None))
    assert "6,532" in " ".join(q.message.edits)
    assert "October 4th, 2026" in " ".join(q.message.edits)


def test_logout_forgets_the_preset_choice(bot, access, ledger, monkeypatch, tmp_path):
    """Logout means ask again next time, even with the preset configured."""
    from conftest import reload_with
    reload_with(monkeypatch, SITE_USERNAME="testuser", SITE_PASSWORD="pw",
                RELAY_STATE_DIR=str(tmp_path))
    from relay.telegram import access as acc
    import relay.telegram as reloaded
    from relay.store import db
    db.init()
    acc.grant(1, "claim")
    reloaded.set_stage(1, "choose_preset")
    q = FakeQuery("cred:preset", chat_id=1)
    upd = FakeUpdate(chat_id=1)
    upd.callback_query = q
    run(reloaded.on_credential_choice(upd, None))
    assert ledger.has_preset_choice(1) is True
    run(reloaded.on_logout(FakeUpdate(chat_id=1), None))
    assert ledger.has_preset_choice(1) is False
    assert reloaded.has_credentials(1) is False
