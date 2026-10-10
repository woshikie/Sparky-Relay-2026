"""The bounded browser hold: read once, confirm without relaunching.

browser_session(chat, hold=True) keeps the browser open up to
HOLD_BROWSER_SECS for that chat; the commit reuses it and closes it on its
own (hold=False) exit. The memory invariant stays: at most one browser ever,
and expiry is lazy -- seen on the next acquire, never by a timer task.

Note on fixtures: as in test_browser_lifecycle, every reference goes through
a fixture. conftest._purge() drops and re-imports the app modules per test,
so a module-level app import would be a stale reference.
"""

import asyncio
import time

import pytest
from conftest import reload_with, run


def reading(available, memory):
    """A budget reading, shaped like the one memory.budget() really returns."""
    return {
        "total_mb": 950.0,
        "available_mb": available,
        "browser_peak_mb": memory.BROWSER_PEAK_MB,
        "min_free_mb": memory.MIN_FREE_MB,
        "can_launch": available >= memory.MIN_FREE_MB,
        "cgroup": True,
        "cgroup_limit_mb": 950.0,
        "cgroup_current_mb": 50.0,
        "cgroup_committed_mb": 50.0,
    }


class FakeRelay:
    """Stands in for Relay so no test can start a browser by accident."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.login_calls = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def login(self, username, password):
        self.login_calls.append((username, password))


@pytest.fixture
def fake_relay(monkeypatch, session, memory):
    fake = FakeRelay()
    monkeypatch.setattr(session, "get_relay", lambda progress=None: fake)
    # asyncio.Lock binds to the first loop that waits on it, and each test
    # runs its own asyncio.run(), so each test needs a fresh one.
    monkeypatch.setattr(session, "_site_lock", asyncio.Lock())
    monkeypatch.setattr(memory, "budget", lambda: reading(900.0, memory))
    monkeypatch.setattr(session.config, "HOLD_BROWSER_SECS", 60)
    return fake


def creds(ledger, session, chat):
    ledger.save_credentials(chat, "testuser", "pw", session.config.TELEGRAM_BOT_TOKEN)


def test_hold_keeps_the_browser_open(session, ledger, fake_relay):
    """The read-phase release with hold=True does not close."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        assert fake_relay.started == 1
        assert fake_relay.stopped == 0

    run(go())


def test_the_commit_reuses_then_closes(session, ledger, fake_relay):
    """Confirm skips the relaunch (one start, two sign-ins) and closes."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        async with session.browser_session(1):
            pass
        assert fake_relay.started == 1
        assert fake_relay.login_calls == [("testuser", "pw")] * 2
        assert fake_relay.stopped == 1

    run(go())


def test_after_commit_the_next_read_relaunches(session, ledger, fake_relay):
    """The commit consumed the hold, so the next read starts fresh."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        async with session.browser_session(1):
            pass
        async with session.browser_session(1, hold=True):
            pass

    run(go())
    assert fake_relay.started == 2
    assert fake_relay.stopped == 1


def test_hold_zero_closes_as_before(session, ledger, fake_relay, monkeypatch):
    """HOLD_BROWSER_SECS=0 is today's close-immediately behaviour."""
    monkeypatch.setattr(session.config, "HOLD_BROWSER_SECS", 0)
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        assert fake_relay.stopped == 1
        async with session.browser_session(1, hold=True):
            pass

    run(go())
    assert fake_relay.started == 2
    assert fake_relay.stopped == 2


def test_expiry_falls_back_to_relaunch(session, ledger, fake_relay, monkeypatch):
    """Past the deadline the next acquire evicts the stale hold and starts."""
    creds(ledger, session, 1)
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        assert fake_relay.stopped == 0
        now[0] = 1061.0
        async with session.browser_session(1):
            pass

    run(go())
    assert fake_relay.started == 2
    assert fake_relay.stopped == 2


def test_date_activity_extends_the_deadline(session, ledger, fake_relay, monkeypatch):
    """A date tap pushes the hold out, so a late Confirm still reuses."""
    creds(ledger, session, 1)
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    async def first():
        async with session.browser_session(1, hold=True):
            pass

    run(first())
    now[0] = 1059.0
    before = session._held_until
    session.refresh_hold(2)
    assert session._held_until == before
    session.refresh_hold(1)
    assert session._held_until == 1059.0 + 60

    now[0] = 1061.0

    async def second():
        async with session.browser_session(1):
            pass

    run(second())
    assert fake_relay.started == 1
    assert fake_relay.stopped == 1


def test_cancel_closes_now(session, ledger, fake_relay):
    """Explicit cancel ends the wait instead of letting the hold lapse."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass

    run(go())
    run(session.close_held(1))
    assert fake_relay.stopped == 1
    run(session.close_held(1))
    assert fake_relay.stopped == 1


def test_close_held_ignores_other_chats(session, ledger, fake_relay):
    """One chat's cancel must not strand another chat's confirmation."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass

    run(go())
    run(session.close_held(2))
    assert fake_relay.stopped == 0

    async def reuse():
        async with session.browser_session(1):
            pass

    run(reuse())
    assert fake_relay.started == 1
    assert fake_relay.stopped == 1


class _CancelChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _CancelMessage:
    def __init__(self, chat_id=1):
        self.chat = _CancelChat(chat_id)
        self.message_id = 200
        self.edits = []
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self


class _CancelQuery:
    def __init__(self, data, chat_id=1):
        self.data = data
        self.answers = []
        self.message = _CancelMessage(chat_id)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.message.edits.append(text)
        return self.message


def test_cancel_closes_held_outside_the_pending_lock(
    bot, access, session, monkeypatch
):
    """Cancel must not call close_held under the pending lock (deadlock).

    The commit path takes the pending lock while holding the site lock
    (site->pending); close_held takes the site lock, so calling it inside
    the pending lock (pending->site) hangs a concurrent Confirm+Cancel on
    one chat forever. The probe records whether the chat's pending lock is
    held when close_held runs: True on the old code, False on the fixed.
    """
    access.grant(1, "claim")
    bot.pending.clear()
    bot.pending.put((1, 100), {
        "path": "/tmp/x.jpg", "steps": 6532, "reported": "6,532",
        "scratch": _CancelMessage(1), "date": None,
    })
    seen = {}

    async def probe(chat_id):
        seen["chat"] = chat_id
        seen["locked"] = bot.pending.lock_for(chat_id).locked()

    monkeypatch.setattr(session, "close_held", probe)
    q = _CancelQuery("ok:cancel", chat_id=1)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.message.chat
    upd.effective_message = q.message
    run(bot.cb_ok(upd, None))
    assert seen == {"chat": 1, "locked": False}
    assert bot.pending.get((1, 100)) is None


def test_a_foreign_chat_evicts_then_launches(session, ledger, fake_relay):
    """One browser ever: another chat's acquire closes the hold first."""
    creds(ledger, session, 1)
    creds(ledger, session, 2)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        async with session.browser_session(2):
            pass

    run(go())
    assert fake_relay.started == 2
    assert fake_relay.stopped == 2


def test_none_borrows_without_disturbing_the_hold(session, ledger, fake_relay):
    """The leaderboard read reuses a live hold: no sign-in, no close."""
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(1, hold=True):
            pass
        async with session.browser_session():
            pass
        assert fake_relay.started == 1
        assert fake_relay.stopped == 0
        assert fake_relay.login_calls == [("testuser", "pw")]
        async with session.browser_session(1):
            pass

    run(go())
    assert fake_relay.started == 1
    assert fake_relay.stopped == 1


def test_a_failure_closes_despite_hold(session, ledger, fake_relay):
    """A failed read must not leave a browser held open."""
    creds(ledger, session, 1)

    async def go():
        with pytest.raises(RuntimeError):
            async with session.browser_session(1, hold=True):
                raise RuntimeError("boom")

    run(go())
    assert fake_relay.stopped == 1

    async def again():
        async with session.browser_session(1, hold=True):
            pass

    run(again())
    assert fake_relay.started == 2
    assert fake_relay.stopped == 1


# --------------------------------------- the refused read releases the hold


class _HoldChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.username = "testuser"


class _HoldScratch:
    async def edit_text(self, text, **kw):
        return None


class _HoldMessage:
    def __init__(self, chat_id=1, message_id=100):
        self.chat = _HoldChat(chat_id)
        self.message_id = message_id
        self.photo = None
        self.document = None
        self.replies = []

    @property
    def chat_id(self):
        """The real Message has this; save_photo() uses it for the filename."""
        return self.chat.id

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return _HoldScratch()

    @property
    def said(self):
        return " ".join(self.replies)


class _HoldUpdate:
    def __init__(self, chat_id=1):
        self.message = _HoldMessage(chat_id)
        self.effective_chat = self.message.chat
        self.effective_message = self.message


class _HoldFile:
    def __init__(self, data):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class _HoldPhotoSize:
    def __init__(self, data):
        self._f = _HoldFile(data)

    async def get_file(self):
        return self._f


def _hold_jpeg(width=400, height=300):
    """A real JPEG, small enough to be a plausible screenshot."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def test_an_implausible_read_releases_the_hold(
    bot, access, session, ledger, fake_relay, tmp_path, monkeypatch
):
    """A refused read never enqueues, so no Confirm ever comes for the hold.

    The read phase arms hold=True unconditionally, then the implausible
    branch used to return without closing: ~640MB pinned for a full
    HOLD_BROWSER_SECS for nothing. Now the refusal closes it: one start,
    one stop, nothing held, nothing queued.
    """
    monkeypatch.setattr(bot.config, "INBOX", str(tmp_path))
    access.grant(1, "claim")
    creds(ledger, session, 1)
    fake_relay.upload = lambda path: (5, "5")

    upd = _HoldUpdate(chat_id=1)
    upd.message.photo = [_HoldPhotoSize(_hold_jpeg())]
    run(bot.on_photo(upd, None))
    assert "plausible" in upd.message.said.lower()
    assert bot.pending.count() == 0
    assert fake_relay.started == 1
    assert fake_relay.stopped == 1
    assert session._held_chat is None


def test_none_hold_is_transient_and_never_parks_a_deadline(
    session, ledger, fake_relay
):
    """browser_session(None, hold=True) normalizes to transient, always.

    None skips sign-in, so there is no identity to hold for: each acquire
    launches and closes (started == stopped per acquire, no reuse, no
    sign-in), and no None-with-deadline entry ever parks in _held_chat.
    Against a live chat-1 hold the None acquire borrows without disturbing
    it: no second browser resident, no deadline change, no eviction -- so a
    later Confirm on chat 1 still reuses the one held browser.
    """
    creds(ledger, session, 1)

    async def go():
        async with session.browser_session(None, hold=True):
            pass
        assert fake_relay.started == 1
        assert fake_relay.stopped == 1
        assert session._held_chat is None
        assert session._held_until == 0.0
        async with session.browser_session(None, hold=True):
            pass
        assert fake_relay.started == 2
        assert fake_relay.stopped == 2
        assert session._held_chat is None
        assert session._held_until == 0.0
        assert fake_relay.login_calls == []

    run(go())

    async def held():
        async with session.browser_session(1, hold=True):
            pass

    run(held())
    assert fake_relay.started == 3
    assert fake_relay.stopped == 2
    assert session._held_chat == 1
    deadline = session._held_until

    async def borrow():
        async with session.browser_session(None, hold=True):
            pass

    run(borrow())
    assert fake_relay.started == 3
    assert fake_relay.stopped == 2
    assert fake_relay.login_calls == [("testuser", "pw")]
    assert session._held_chat == 1
    assert session._held_until == deadline

    async def reuse():
        async with session.browser_session(1):
            pass

    run(reuse())
    assert fake_relay.started == 3
    assert fake_relay.stopped == 3


# ------------------------------------------------------------- the knob


def test_hold_defaults_to_sixty_seconds(monkeypatch, tmp_path):
    monkeypatch.delenv("HOLD_BROWSER_SECS", raising=False)
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.HOLD_BROWSER_SECS == 60
    assert "HOLD_BROWSER_SECS" in cfg.KNOWN_KEYS


def test_hold_parses_seconds(monkeypatch, tmp_path):
    cfg = reload_with(
        monkeypatch, HOLD_BROWSER_SECS="90", RELAY_STATE_DIR=str(tmp_path)
    )
    assert cfg.HOLD_BROWSER_SECS == 90


def test_hold_zero_is_close_immediately(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, HOLD_BROWSER_SECS="0", RELAY_STATE_DIR=str(tmp_path))
    assert cfg.HOLD_BROWSER_SECS == 0


def test_hold_garbage_falls_back_to_default(monkeypatch, tmp_path):
    cfg = reload_with(
        monkeypatch, HOLD_BROWSER_SECS="soon", RELAY_STATE_DIR=str(tmp_path)
    )
    assert cfg.HOLD_BROWSER_SECS == 60


def test_hold_negative_floors_at_zero(monkeypatch, tmp_path):
    cfg = reload_with(
        monkeypatch, HOLD_BROWSER_SECS="-5", RELAY_STATE_DIR=str(tmp_path)
    )
    assert cfg.HOLD_BROWSER_SECS == 0
