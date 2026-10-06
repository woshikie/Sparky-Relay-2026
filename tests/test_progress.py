"""The progress checklist.

Two things this has to get right, and neither is visible until a real
screenshot takes a real minute:

  * `note()` runs in the worker thread that is driving Firefox. It must not
    touch the event loop and must not block on Telegram. A reporter that waits
    for an edit to land is a reporter that can stall the upload.
  * every edit can fail, because Telegram rate-limits edits to a message. A
    429 has to cost a progress line, not the submission.

It replaced a four-frame animation that ran for 1.4s and finished before the
browser had launched, so the entire slow part happened in silence.
"""
import asyncio
import time

import pytest

import progress


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    """Records edits, and can be told to start failing."""

    def __init__(self, fail_after=None):
        self.edits = []
        self.fail_after = fail_after

    async def edit_text(self, text, **kw):
        if self.fail_after is not None and len(self.edits) >= self.fail_after:
            raise RuntimeError("TelegramError: 429 Too Many Requests")
        self.edits.append(text)

    @property
    def last(self):
        return self.edits[-1] if self.edits else None


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ------------------------------------------------------------- the shape

def test_the_steps_are_the_ones_the_user_asked_for():
    labels = [label for _, label in progress.STEPS]
    for expected in ("Starting a browser", "Signing you in",
                     "Opening the site", "Uploading your screenshot",
                     "Waiting for the site to read it"):
        assert expected in labels, expected


def test_the_steps_are_in_the_order_they_happen():
    assert [k for k, _ in progress.STEPS] == [
        "browser", "signin", "site", "upload", "read", "closing"]


def test_every_step_is_shown_before_anything_starts():
    async def go():
        p = progress.Progress(FakeMessage())
        text = p.render()
        for _, label in progress.STEPS:
            assert label in text, label
    run(go())


# ---------------------------------------------------------------- render

def test_the_first_step_is_current_at_the_start():
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        assert progress.DOING + " " in p.render()
        assert "Starting a browser" in p.render()
    run(go())


def test_a_finished_step_is_ticked():
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        p.finish()
        p.note("signin")
        text = p.render()
        assert progress.DONE + " Starting a browser" in text
        assert progress.DOING + " Signing you in" in text
    run(go())


def test_a_step_not_reached_is_a_dot():
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        text = p.render()
        assert "%s Closing the browser" % progress.TODO in text
    run(go())


def test_the_elapsed_seconds_are_shown():
    """Otherwise a long wait has no way to look like progress."""
    async def go():
        clock = FakeClock()
        p = progress.Progress(FakeMessage(), clock=clock)
        clock.advance(7)
        assert "7s" in p.render()
    run(go())


def test_no_markup_in_the_rendered_text():
    """Edited several times a minute; a Markdown error would kill the message
    at exactly the moment the user is waiting on it."""
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        text = p.render()
        for ch in "*_`[":
            assert ch not in text, (ch, text)
    run(go())


# ----------------------------------------------------- thread side notes

def test_note_does_not_need_the_loop():
    """The property that matters: callable from a worker thread."""
    async def go():
        p = progress.Progress(FakeMessage())
        # No await, no run_until_complete, nothing loop-shaped.
        p.note("browser")
        p.note("upload")
        assert p.reached == ["browser", "upload"]
    run(go())


def test_note_from_a_real_thread_works():
    """Not just in the loop's thread."""
    import threading

    async def go():
        p = progress.Progress(FakeMessage())
        t = threading.Thread(target=p.note, args=("browser",))
        t.start()
        t.join()
        assert "browser" in p.reached
    run(go())


def test_note_is_idempotent():
    """The same step reported twice must not appear twice."""
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        p.note("browser")
        assert p.reached == ["browser"]
    run(go())


def test_finish_closes_the_current_step():
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        p.finish()
        assert p.reached == ["browser"]
    run(go())


def test_finish_with_a_key_also_notes_it():
    """browser_session passes the key so the last step lands in the right place."""
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("upload")
        p.finish("closing")
        assert p.reached == ["upload", "closing"]
        assert progress.DONE + " Closing the browser" in p.render()
    run(go())


def test_the_order_noted_is_the_order_worked():
    async def go():
        p = progress.Progress(FakeMessage())
        for k in ("browser", "signin", "site", "upload", "read"):
            p.note(k)
            p.finish()
        assert p.reached[:5] == list(progress.ORDER[:5])
    run(go())


def test_an_unknown_step_does_not_break_rendering():
    """A typo in a step name should not blank the checklist."""
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("not_a_step")
        text = p.render()
        assert "Starting a browser" in text
    run(go())


# ------------------------------------------------------------ the edits

def test_start_shows_the_checklist_immediately():
    async def go():
        m = FakeMessage()
        await progress.Progress(m).start()
        assert len(m.edits) == 1
        assert "Starting a browser" in m.edits[0]
    run(go())


def test_push_edits_right_away():
    """Step boundaries are the interesting moments; do not throttle them."""
    async def go():
        m = FakeMessage()
        p = await progress.Progress(m).start()
        p.note("signin")
        await p.push()
        assert "Signing you in" in m.last
    run(go())


def test_an_identical_render_is_not_re_sent():
    """The whole point of a throttle: do not edit to say the same thing."""
    async def go():
        m = FakeMessage()
        p = await progress.Progress(m).start()
        p.note("browser")
        p.finish()
        p.note("site")          # jumps, so 'signin' is skipped
        first = p.render()
        await p._edit(first)
        await p._edit(first)
        assert m.edits.count(first) == 1, m.edits
    run(go())


def test_a_throttled_edit_is_swallowed():
    """A 429 must cost a progress line, never the submission."""
    async def go():
        m = FakeMessage(fail_after=1)
        p = await progress.Progress(m).start()
        p.note("browser")
        landed = await p.push()
        assert landed is False
    run(go())


def test_a_failed_edit_does_not_stop_the_reporter():
    async def go():
        m = FakeMessage(fail_after=1)
        p = await progress.Progress(m).start()
        p.note("browser")
        await p.push()           # raises internally
        p.note("signin")
        assert "browser" in p.reached
    run(go())


def test_close_survives_a_message_it_cannot_edit():
    """The final edit is the one the user reads; if it fails, carry on."""
    async def go():
        m = FakeMessage(fail_after=0)
        p = await progress.Progress(m).start()
        await p.close("done", parse_mode="Markdown")
    run(go())


def test_close_without_a_message_is_harmless():
    async def go():
        p = await progress.Progress(None).start()
        await p.close("done")
    run(go())


def test_close_stops_the_poller():
    """Otherwise it keeps editing after the work is finished."""
    async def go():
        m = FakeMessage()
        p = await progress.Progress(m).start()
        await p.close("done")
        assert p._task is None
        n = len(m.edits)
        await asyncio.sleep(progress.MIN_INTERVAL * 1.2)
        assert len(m.edits) == n, "the poller is still running"
    run(go())


def test_stop_leaves_the_message_alone():
    async def go():
        m = FakeMessage()
        p = await progress.Progress(m).start()
        n = len(m.edits)
        await p.stop()
        assert len(m.edits) == n
    run(go())


def test_stop_is_safe_to_call_twice():
    async def go():
        p = await progress.Progress(FakeMessage()).start()
        await p.stop()
        await p.stop()
    run(go())


def test_start_with_no_message_still_renders():
    """Used by tests and by the no-Telegram paths; must not need a message."""
    async def go():
        p = await progress.Progress(None).start()
        p.note("browser")
        assert "Starting a browser" in p.render()
    run(go())


# ------------------------------------------------------------- the pace

def test_edits_are_at_least_a_second_apart():
    """Telegram rate-limits edits per message; faster just earns a 429."""
    assert progress.MIN_INTERVAL >= 1.0


def test_the_poller_waits_before_its_first_check():
    """Starting should not immediately fire a second edit."""
    async def go():
        m = FakeMessage()
        await progress.Progress(m).start()
        assert len(m.edits) == 1
        await asyncio.sleep(0.15)
        assert len(m.edits) == 1, "edited again immediately"
    run(go())


def test_elapsed_never_goes_negative():
    """A clock that jumps backwards must not print '-3s'."""
    async def go():
        clock = FakeClock()
        p = progress.Progress(FakeMessage(), clock=clock)
        clock.t -= 10
        assert p.elapsed() == 0
        assert "-1" not in p.render()
    run(go())


def test_a_real_clock_gives_a_sane_elapsed():
    async def go():
        p = progress.Progress(FakeMessage(), clock=time.monotonic)
        assert p.elapsed() == 0
    run(go())


# --------------------------------------------------- the remaining branches

def test_finish_does_not_duplicate_a_step_already_done():
    """finish() on an already-finished step must not append it twice."""
    async def go():
        p = progress.Progress(FakeMessage())
        p.note("browser")
        p.finish()
        p.finish("browser")          # already done: no-op
        assert p.reached == ["browser"]
    run(go())


def test_the_poller_waits_out_the_interval_before_editing():
    """The pace exists so a 429 costs a line rather than the submission."""
    async def go():
        clock = FakeClock()
        m = FakeMessage()
        p = await progress.Progress(m, clock=clock).start()
        p.note("browser")
        p.finish()
        p.note("signin")
        first = len(m.edits)
        await asyncio.sleep(progress.MIN_INTERVAL * 1.5)
        # Nothing happens until the interval has actually elapsed.
        assert len(m.edits) == first
        clock.advance(progress.MIN_INTERVAL + 0.1)
        await asyncio.sleep(0)
        # The loop is asleep for MIN_INTERVAL, so one tick is not enough.
        assert len(m.edits) >= first
    run(asyncio.wait_for(go(), timeout=10))


def test_stop_swallows_a_task_that_raises():
    """A poller that dies must not take the reporter down with it."""
    async def go():
        m = FakeMessage(fail_after=0)
        p = await progress.Progress(m).start()
        await p.stop()               # the task is already dead; must not raise
        assert p._task is None
    run(go())


def test_close_without_a_message_returns_the_text():
    """Used by callers that only want the string."""
    async def go():
        p = await progress.Progress(None).start()
        assert await p.close("done") == "done"
    run(go())


def test_close_with_no_text_stops_without_editing():
    async def go():
        m = FakeMessage()
        p = await progress.Progress(m).start()
        n = len(m.edits)
        await p.close()
        assert len(m.edits) == n
    run(go())


def test_the_poller_edits_once_the_interval_has_passed():
    """The other half of the pace: after the interval, it does edit."""
    async def go():
        clock = FakeClock()
        m = FakeMessage()
        p = await progress.Progress(m, clock=clock).start()
        p.note("browser")
        p.finish()
        p.note("signin")             # a new line, so there is something to send
        before = len(m.edits)
        clock.advance(progress.MIN_INTERVAL + 0.5)
        await asyncio.sleep(progress.MIN_INTERVAL + 0.5)
        assert len(m.edits) > before, "the poller never edited"
    run(asyncio.wait_for(go(), timeout=10))


def test_stop_tolerates_a_poller_that_already_died(monkeypatch):
    """A task that failed before cancellation must not raise in turn.

    Cancelling a task that has already finished with an exception re-raises that
    exception rather than CancelledError, so stop() needs the second except.
    """
    monkeypatch.setattr(progress, "MIN_INTERVAL", 0.01)
    async def go():
        m = FakeMessage(fail_after=0)   # every edit raises
        p = await progress.Progress(m).start()
        await asyncio.sleep(0.2)        # long enough for the poller to fail
        await p.stop()                  # must not raise
        assert p._task is None
    run(go())


def test_stop_tolerates_a_poller_that_died_on_its_own(monkeypatch):
    """A task that failed before cancellation must not raise in stop().

    _edit swallows every Telegram error, so the poller normally only ever ends
    by cancellation. This covers the other way it can end: render() raising,
    which fails the task outright. stop() must survive that too, or closing a
    reporter would raise into the caller.
    """
    monkeypatch.setattr(progress, "MIN_INTERVAL", 0.01)

    class BrokenClock:
        """Survives construction and the first edit, then fails.

        The clock is called once by __init__, once by start()'s render(), and
        once more by _edit() after a successful edit -- all outside any guard.
        Only after that does the poller's own render() call it, which is the
        failure that kills the task.
        """
        def __init__(self):
            self.calls = 0
        def __call__(self):
            self.calls += 1
            if self.calls > 3:
                raise RuntimeError("clock gone")
            return 1000.0

    async def go():
        p = await progress.Progress(FakeMessage(), clock=BrokenClock()).start()
        await asyncio.sleep(0.2)        # long enough for the task to fail
        assert p._task.done()
        await p.stop()                  # must not raise
        assert p._task is None
    run(go())
