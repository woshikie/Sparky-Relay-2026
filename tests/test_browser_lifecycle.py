"""The browser lifecycle: what `browser_session` promises.

Two guarantees, both easy to break in ways the photo flow would not catch until
someone sent a real screenshot:

  * the browser is closed afterwards, whatever happens inside the block
  * it is not started at all when the host cannot spare the RAM

The bug this file was written for: the decorator was `contextlib.contextmanager`
on an `async def`, so `async with browser_session()` raised
"'_GeneratorContextManager' object does not support the asynchronous context
manager protocol" on the first real screenshot, with every test green. No unit
test entered the block with a real browser, so the protocol went unchecked.

Note on fixtures: conftest._purge() drops these modules and re-imports them
before every test, so a module-level `import bot` is a *stale* reference.
Monkeypatching it silently does nothing to the code actually running — which is
how this file spent 50 seconds a run with a real Firefox in it. Every reference
here goes through a fixture.
"""
import asyncio
import inspect
import importlib

import pytest


def run(coro):
    return asyncio.run(coro)


def reading(available, memory):
    """A budget reading, shaped like the one memory.budget() really returns.

    Patching memory.budget rather than memory.require_memory is deliberate:
    this way the *real* require_memory still runs and the actual raise-vs-
    proceed decision is what is under test.
    """
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
def fake_relay(monkeypatch, bot, memory):
    fake = FakeRelay()
    monkeypatch.setattr(bot, "get_relay", lambda: fake)
    # asyncio.Lock binds itself to the first loop that waits on it, and each
    # test here runs its own asyncio.run(). Reuse across loops hangs rather than
    # fails, so hand each test a fresh one. In production the lock is created
    # once and used by one long-lived loop.
    monkeypatch.setattr(bot, "_site_lock", asyncio.Lock())
    monkeypatch.setattr(memory, "budget",
                        lambda: reading(900.0, memory))
    return fake


# ------------------------------------------------- the protocol, first

def test_browser_session_is_an_async_context_manager(bot):
    """The regression, asserted directly and bluntly.

    Nothing else in the suite would notice: entering the block requires a
    browser, and every other test here fakes one.
    """
    cm = bot.browser_session()
    assert hasattr(cm, "__aenter__")
    assert hasattr(cm, "__aexit__")


def test_it_is_not_a_sync_context_manager(bot):
    """The exact bug: a sync wrapper around an async generator."""
    import contextlib
    assert not isinstance(bot.browser_session(),
                          contextlib._GeneratorContextManager)


def test_the_body_is_an_async_generator(bot):
    """asynccontextmanager wraps the generator, so unwrap before checking."""
    assert inspect.isasyncgenfunction(inspect.unwrap(bot.browser_session))


# ------------------------------------------------------------ happy path

def test_it_yields_a_started_relay(bot, fake_relay):
    async def go():
        async with bot.browser_session() as r:
            assert r is fake_relay
            assert fake_relay.started == 1
            assert fake_relay.stopped == 0
    run(go())


def test_it_closes_the_browser_on_the_way_out(bot, fake_relay):
    async def go():
        async with bot.browser_session():
            pass
        assert fake_relay.stopped == 1
    run(go())


def test_it_closes_the_browser_even_when_the_body_raises(bot, fake_relay):
    async def go():
        with pytest.raises(RuntimeError):
            async with bot.browser_session():
                raise RuntimeError("boom")
        assert fake_relay.stopped == 1
    run(go())


def test_it_closes_the_browser_even_when_the_body_is_cancelled(bot, fake_relay):
    async def go():
        with pytest.raises(asyncio.CancelledError):
            async with bot.browser_session():
                raise asyncio.CancelledError()
        assert fake_relay.stopped == 1
    run(go())


def test_the_log_says_what_it_did(bot, fake_relay, capsys):
    async def go():
        async with bot.browser_session():
            pass
    run(go())
    out = capsys.readouterr().out
    assert "launching browser" in out
    assert "closing browser" in out


def test_the_log_reports_the_headroom(bot, fake_relay, capsys):
    """A number the operator can act on, not just "launching"."""
    async def go():
        async with bot.browser_session():
            pass
    run(go())
    assert "900MB free" in capsys.readouterr().out


# ---------------------------------------------------------- one at a time

def test_two_sessions_do_not_overlap(bot, fake_relay):
    """The lock is the point: one browser, so two screenshots cannot race.

    Sequential uploads are not the hazard — two at once is, and two at once is
    what happens if someone taps Submit twice.
    """
    order = []

    async def one():
        async with bot.browser_session():
            order.append("in-1")
            await asyncio.sleep(0.05)
            order.append("out-1")

    async def two():
        async with bot.browser_session():
            order.append("in-2")
            order.append("out-2")

    async def go():
        await asyncio.gather(one(), two())

    run(go())
    assert order in (["in-1", "out-1", "in-2", "out-2"],
                     ["in-2", "out-2", "in-1", "out-1"]), order


def test_the_lock_is_released_even_after_a_failure(bot, fake_relay):
    """Otherwise one bad screenshot would wedge the bot for good."""

    async def go():
        with pytest.raises(RuntimeError):
            async with bot.browser_session():
                raise RuntimeError("boom")
        async with bot.browser_session():
            assert fake_relay.started == 2
    run(asyncio.wait_for(go(), timeout=10))


# --------------------------------------------------------- memory refusal

@pytest.fixture
def starved(monkeypatch, memory):
    monkeypatch.setattr(memory, "budget",
                        lambda: reading(100.0, memory))
    return memory


def test_it_refuses_when_memory_is_short(bot, fake_relay, starved):
    async def go():
        with pytest.raises(starved.InsufficientMemory):
            async with bot.browser_session():
                pass
    run(go())


def test_it_does_not_launch_a_browser_it_cannot_afford(bot, fake_relay, starved):
    """The whole point: refuse, rather than start and get OOM-killed."""
    async def go():
        with pytest.raises(starved.InsufficientMemory):
            async with bot.browser_session():
                pass
    run(go())
    assert fake_relay.started == 0


def test_no_browser_is_closed_if_none_was_opened(bot, fake_relay, starved):
    """The refusal path must not call stop() on a browser that never started."""
    async def go():
        with pytest.raises(starved.InsufficientMemory):
            async with bot.browser_session():
                pass
    run(go())
    assert fake_relay.stopped == 0


def test_the_refusal_names_the_numbers(bot, fake_relay, starved):
    """Actionable: how much it wanted, how much it had."""
    async def go():
        with pytest.raises(starved.InsufficientMemory) as exc:
            async with bot.browser_session():
                pass
        assert str(starved.MIN_FREE_MB) in str(exc.value)
        assert "100" in str(exc.value)
    run(go())


def test_the_refusal_carries_the_reading(bot, fake_relay, starved):
    """So the reply can show the headroom rather than just say 'no'."""
    async def go():
        with pytest.raises(starved.InsufficientMemory) as exc:
            async with bot.browser_session():
                pass
        assert exc.value.report["available_mb"] == 100.0
    run(go())


def test_the_lock_is_released_after_a_refusal(bot, fake_relay, starved, monkeypatch):
    """A refusal must not block the next attempt, once memory frees up."""
    async def go():
        with pytest.raises(starved.InsufficientMemory):
            async with bot.browser_session():
                pass
        monkeypatch.setattr(starved, "budget",
                            lambda: reading(900.0, starved))
        async with bot.browser_session():
            assert fake_relay.started == 1
    run(asyncio.wait_for(go(), timeout=10))


def test_one_exception_type_for_a_short_of_memory(bot):
    """There used to be two — InsufficientMemory wrapped in BrowserUnavailable
    — and every caller had to catch both for one condition."""
    assert "BrowserUnavailable" not in inspect.getsource(bot)


# ------------------------------------------------------------ credentials

def test_sign_in_uses_the_chats_own_credentials(bot, access, ledger,
                                               fake_relay):
    """A submission belongs to whoever supplied the credentials."""
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "testpass123",
                            bot.config.TELEGRAM_BOT_TOKEN)
    run(bot.sign_in(1))
    assert fake_relay.login_calls == [("testuser", "testpass123")]


def test_sign_in_without_credentials_names_the_chat(bot, access, fake_relay):
    access.grant(1, "claim")
    with pytest.raises(bot._NoCredentials):
        run(bot.sign_in(1))
    assert fake_relay.login_calls == []


def test_a_rotated_token_is_its_own_failure(bot, access, ledger, fake_relay):
    """Distinct from 'never signed in': the reply differs, so the type must."""
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "pw", "111:OLDTOKEN")
    vault = importlib.import_module("vault")
    with pytest.raises(vault.DecryptionFailed):
        run(bot.sign_in(1))
    assert fake_relay.login_calls == []


def test_sign_in_does_not_start_the_browser(bot, access, ledger, fake_relay):
    """The caller owns the lifetime; sign_in only logs in."""
    access.grant(1, "claim")
    ledger.save_credentials(1, "testuser", "pw",
                            bot.config.TELEGRAM_BOT_TOKEN)
    run(bot.sign_in(1))
    assert fake_relay.started == 0, "browser_session owns start/stop"
    assert fake_relay.stopped == 0
