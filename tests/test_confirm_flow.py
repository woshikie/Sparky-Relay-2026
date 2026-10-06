"""The date-selection callbacks, and the overwrite guard they feed.

This is the highest-stakes logic in the bot after the commit itself: choosing
the wrong Activity Date files a Submission under the wrong day, and `cb_ok`
overwrites silently. The guard is one-directional by design — a downgrade is
always challenged, an upgrade never is — so most of these tests are about which
direction is silent and which is not.
"""
import asyncio
import contextlib
import datetime

import pytest

from conftest import run, FAKE_TOKEN


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.username = "testuser"
        self.first_name = "Test"


class FakeMessage:
    def __init__(self, chat_id=1, message_id=200, reply_to_id=None):
        self.chat = FakeChat(chat_id)
        self.message_id = message_id
        self.text = None
        self.replies = []
        self.edits = []
        self.markup = None
        self.deleted = False
        self.photo = None
        self.document = None
        self.reply_to_message = FakeMessage(chat_id, reply_to_id) \
            if reply_to_id else None

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

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, chat_id=1, message_id=200, reply_to_id=None):
        self.data = data
        self.answers = []
        self.message = FakeMessage(chat_id, message_id, reply_to_id)
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
def pending(bot, access):
    """A granted chat with one Screenshot awaiting a date."""
    access.grant(1, "claim")
    bot.pending.clear()
    bot.pending.put((1, 100), {
        "path": "/tmp/x.jpg", "steps": 6532, "reported": "6,532",
        "scratch": FakeMessage(), "date": None,
    })
    return bot


def press(bot, data, chat_id=1):
    q = FakeQuery(data, chat_id=chat_id)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    run(bot.cb_date(upd, None))
    return q


# --------------------------------------------------------------- the dates

def test_today_selects_today(pending):
    press(pending, "dt:today")
    st = pending.pending.get((1, 100))
    assert st["date"] == pending.sg_today()


def test_yesterday_selects_yesterday(pending):
    press(pending, "dt:yday")
    st = pending.pending.get((1, 100))
    assert st["date"] == pending.sg_today() - datetime.timedelta(days=1)


def test_a_calendar_day_is_used_verbatim(pending):
    press(pending, "dt:day:2026-10-03")
    assert pending.pending.get((1, 100))["date"] == datetime.date(2026, 10, 3)


def test_the_confirmation_shows_the_date_chosen(pending):
    """A misfile is invisible until someone checks the board, so echo it."""
    q = press(pending, "dt:day:2026-10-03")
    assert "2026-10-03" in " ".join(q.message.edits)


def test_the_confirmation_shows_the_date_in_words(pending):
    q = press(pending, "dt:day:2026-10-03")
    assert "October 3rd, 2026" in " ".join(q.message.edits)


def test_today_reads_naturally_in_the_confirmation(pending):
    q = press(pending, "dt:today")
    assert "Today" in " ".join(q.message.edits) or \
        pending.sg_today().strftime("%B") in " ".join(q.message.edits)


def test_going_back_restores_the_default_keyboard(pending):
    q = press(pending, "dt:back")
    assert q.message.markup is not None
    assert pending.pending.get((1, 100))["date"] is None


def test_opening_the_picker_shows_a_calendar(pending):
    q = press(pending, "dt:pick:2026:10")
    assert "date" in " ".join(q.message.edits).lower()
    assert q.message.markup is not None


def test_paging_the_calendar_forwards(pending):
    q = press(pending, "dt:next:2026:10")
    assert q.message.markup is not None


def test_paging_the_calendar_back(pending):
    q = press(pending, "dt:prev:2026:10")
    assert q.message.markup is not None


def test_a_dead_button_is_a_no_op(pending):
    q = press(pending, "dt:none")
    assert len(q.answers) == 1
    assert q.answers[0][1] is False
    assert pending.pending.get((1, 100))["date"] is None


def test_an_unrecognised_callback_is_a_no_op(pending):
    q = press(pending, "dt:whatever")
    assert pending.pending.get((1, 100))["date"] is None


# --------------------------------------------------------- the overwrite guard

def test_a_first_time_date_offers_plain_confirmation(pending):
    q = press(pending, "dt:today")
    assert "Confirm" in q.message.markup.inline_keyboard[0][0].text


def test_an_upgrade_is_not_challenged(pending, ledger):
    """A later, fuller screenshot of the same day is the normal case."""
    iso = pending.sg_today().isoformat()
    ledger.record(iso, 6532)
    press(pending, "dt:today")            # same number: nothing either way
    assert "Overwrite" not in str(pending.words)


def test_a_downgrade_is_challenged(pending, ledger):
    """The asymmetric guard: lower means something went wrong upstream."""
    pending.pending.get((1, 100))["steps"] = 700
    pending.pending.get((1, 100))["reported"] = "700"
    iso = pending.sg_today().isoformat()
    ledger.record(iso, 6532)
    q = press(pending, "dt:today")
    texts = " ".join(q.message.edits)
    assert "6,532" in texts          # what is currently recorded
    assert "700" in texts            # what the screenshot read
    assert "Overwrite" in q.message.markup.inline_keyboard[0][0].text


def test_the_downgrade_warning_is_answered_with_an_alert(pending, ledger):
    pending.pending.get((1, 100))["steps"] = 700
    ledger.record(pending.sg_today().isoformat(), 6532)
    q = press(pending, "dt:today")
    assert any(a[1] for a in q.answers), "the popup should be an alert"


def test_a_downgrade_offers_no_plain_confirm(pending, ledger):
    """There must be no path that silently accepts the lower number."""
    pending.pending.get((1, 100))["steps"] = 700
    ledger.record(pending.sg_today().isoformat(), 6532)
    q = press(pending, "dt:today")
    assert "Confirm" not in str(q.message.markup)


def test_the_guard_is_per_date_not_global(pending, ledger):
    """Yesterday's record must not block today's."""
    yesterday = (pending.sg_today() - datetime.timedelta(days=1)).isoformat()
    ledger.record(yesterday, 6532)
    pending.pending.get((1, 100))["steps"] = 700
    q = press(pending, "dt:today")       # today, not yesterday
    assert "Overwrite" not in str(q.message.markup)


def test_an_equal_number_is_not_challenged(pending, ledger):
    """Re-uploading the same screenshot is harmless; do not make it feel risky."""
    ledger.record(pending.sg_today().isoformat(), 6532)
    q = press(pending, "dt:today")
    assert "Overwrite" not in str(q.message.markup)


# -------------------------------------------------------------- housekeeping

def test_an_expired_request_says_so(pending):
    pending.pending.clear()
    q = press(pending, "dt:today")
    assert "expired" in " ".join(q.message.edits).lower()


def test_an_unauthorised_chat_gets_no_calendar(pending, access):
    access.deny(2, "spam")
    q = press(pending, "dt:today", chat_id=2)
    assert q.answers and q.answers[0][1], "an alert, not a silent change"


# --------------------------------------------------------------- confirm / cancel

def test_cancel_discards_the_pending_request(pending):
    upd = type("U", (), {})()
    upd.callback_query = FakeQuery("ok:cancel")
    upd.effective_chat = upd.callback_query.effective_chat
    upd.effective_message = upd.callback_query.effective_message
    run(pending.cb_ok(upd, None))
    assert pending.pending.get((1, 100)) is None


def test_cancel_says_nothing_was_recorded(pending):
    upd = type("U", (), {})()
    upd.callback_query = FakeQuery("ok:cancel")
    upd.effective_chat = upd.callback_query.effective_chat
    upd.effective_message = upd.callback_query.effective_message
    run(pending.cb_ok(upd, None))
    assert "recorded" in " ".join(upd.callback_query.message.edits).lower()


def test_confirm_without_a_date_is_refused(pending):
    """Confirm must not be reachable before a date exists."""
    upd = type("U", (), {})()
    upd.callback_query = FakeQuery("ok:go")
    upd.effective_chat = upd.callback_query.effective_chat
    upd.effective_message = upd.callback_query.effective_message
    run(pending.cb_ok(upd, None))
    assert "Pick a date" in str(upd.callback_query.answers[0][0])
    assert pending.pending.get((1, 100)) is not None, "the request survives to be retried"


def test_an_expired_confirm_says_so(pending):
    pending.pending.clear()
    upd = type("U", (), {})()
    upd.callback_query = FakeQuery("ok:go")
    upd.effective_chat = upd.callback_query.effective_chat
    upd.effective_message = upd.callback_query.effective_message
    run(pending.cb_ok(upd, None))
    assert "expired" in " ".join(upd.callback_query.message.edits).lower()

# ------------------------------------------- the second OCR pass in cb_ok

def _confirm(bot, chat_id=1):
    q = FakeQuery("ok:go", chat_id=chat_id)
    upd = type("U", (), {})()
    upd.callback_query = q
    upd.effective_chat = q.effective_chat
    upd.effective_message = q.effective_message
    return q, upd


def test_a_changed_second_read_aborts_the_commit(pending, monkeypatch, session):
    """The re-read must agree with what the user confirmed.

    The browser is closed after the first read to free RAM, so Commit re-opens
    it and re-uploads. If the site reads a different number the second time,
    the Screenshot is not the one the user agreed to, and nothing is written.
    """
    pending.pending.get((1, 100))["date"] = datetime.date(2026, 10, 4)
    pending.pending.get((1, 100))["iso"] = "2026-10-04"
    pending.pending.get((1, 100))["label"] = "October 4th, 2026"

    class R:
        def upload(self, path, mode="steps"):
            return 9999, "9,999"
        def set_date(self, d):
            pass
        def commit(self, steps):
            return "should not get here"

    @contextlib.asynccontextmanager
    async def fake_session(chat_id=None, progress=None):
        yield R()
    monkeypatch.setattr(session, "browser_session", fake_session)
    async def fake_sign_in(chat_id, progress=None):
        return None
    monkeypatch.setattr(session, "sign_in", fake_sign_in)

    q, upd = _confirm(pending)
    run(pending.cb_ok(upd, None))
    assert pending.pending.get((1, 100)) is None, "the request is discarded"
    assert "changed" in q.answers[0][0]


def test_an_unchanged_second_read_proceeds_to_commit(pending, monkeypatch, session):
    """The same number twice: the commit goes ahead."""
    pending.pending.get((1, 100))["date"] = datetime.date(2026, 10, 4)
    pending.pending.get((1, 100))["iso"] = "2026-10-04"
    pending.pending.get((1, 100))["label"] = "October 4th, 2026"

    committed = []

    class R:
        def upload(self, path, mode="steps"):
            return 6532, "6,532"
        def set_date(self, d):
            pass
        def commit(self, steps):
            committed.append(steps)
            return "Recorded 6,532 steps for 4 Oct 2026"

    @contextlib.asynccontextmanager
    async def fake_session(chat_id=None, progress=None):
        yield R()
    monkeypatch.setattr(session, "browser_session", fake_session)
    async def fake_sign_in(chat_id, progress=None):
        return None
    monkeypatch.setattr(session, "sign_in", fake_sign_in)

    q, upd = _confirm(pending)
    run(pending.cb_ok(upd, None))
    assert committed == [6532]
    # The final word is a reply, not an edit: the site's own text is the evidence.
    assert "Recorded" in q.message.said


def test_the_commit_reports_what_the_site_said(pending, monkeypatch, session):
    """After Commit the site's own words are the evidence, not ours."""
    from relay.store import ledger as led
    pending.pending.get((1, 100))["date"] = datetime.date(2026, 10, 4)
    pending.pending.get((1, 100))["iso"] = "2026-10-04"
    pending.pending.get((1, 100))["label"] = "October 4th, 2026"

    class R:
        def upload(self, path, mode="steps"):
            return 6532, "6,532"
        def set_date(self, d):
            pass
        def commit(self, steps):
            return "Recorded 6,532 steps for 4 Oct 2026"

    @contextlib.asynccontextmanager
    async def fake_session(chat_id=None, progress=None):
        yield R()
    monkeypatch.setattr(session, "browser_session", fake_session)
    async def fake_sign_in(chat_id, progress=None):
        return None
    monkeypatch.setattr(session, "sign_in", fake_sign_in)

    q, upd = _confirm(pending)
    run(pending.cb_ok(upd, None))
    row = led.last_submission("2026-10-04")
    assert row and row["steps"] == 6532
    assert "Recorded 6,532 steps" in q.message.said
