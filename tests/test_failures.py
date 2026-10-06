"""The shared failure mapping.

on_photo, on_sync, and cb_ok used to carry near-identical six-branch except
chains. The mapping lives in failures.explain() now; these tests pin it, so a
change to what the user reads in any failure is a deliberate edit here rather
than drift across three handlers.

Each test drives explain() directly with a fake message. What is asserted is
the contract the callers rely on: the reply text, the callback answer triple,
and whether the credentials prompt is started.
"""
import pytest


@pytest.fixture
def failures():
    """Fresh per test, like every other app module.

    failures.py binds `memory` at import for its isinstance mapping, so a
    module-level import would compare against the previous test's classes.
    """
    import importlib
    return importlib.import_module("relay.telegram.failures")


@pytest.fixture
def _NoCredentials():
    import importlib
    return importlib.import_module(
        "relay.telegram.prompts")._NoCredentials


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.prompts_started = 0

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self

    async def start_prompt(self):
        self.prompts_started += 1

    @property
    def said(self):
        return " ".join(self.replies)


def run(coro):
    import asyncio
    return asyncio.run(coro)


def explained(failures, exc, operation="Upload", prompt=True):
    msg = FakeMessage()
    start = msg.start_prompt if prompt else None
    out = run(failures.explain(msg, exc, operation, start_prompt=start))
    return msg, out


# ------------------------------------------------------- the two fixes

def test_no_credentials_asks_for_them(failures, _NoCredentials):
    """The fix is supplying credentials, so the prompt starts."""
    msg, (alert, alarm, log_line) = explained(failures, _NoCredentials("none stored"))
    assert "username" in msg.said.lower() or "login" in msg.said.lower()
    assert msg.prompts_started == 1
    assert alert == "credentials needed" and alarm is True
    assert log_line is None, "routine: said out loud, left out of the log"


def test_a_rotated_token_says_the_password_is_gone(failures):
    import relay.store.vault as vault
    msg, (alert, alarm, log_line) = explained(failures,
        vault.DecryptionFailed("the key no longer fits"))
    assert "gone" in msg.said.lower() or "again" in msg.said.lower()
    assert msg.prompts_started == 1
    assert alert == "stored credentials unreadable" and alarm is True
    assert log_line is None


def test_without_a_prompt_nothing_starts_one(failures, _NoCredentials):
    """The caller decides whether a prompt makes sense, not the mapping."""
    msg, _ = explained(failures, _NoCredentials("none"), prompt=False)
    assert msg.prompts_started == 0
    assert "username" in msg.said.lower() or "login" in msg.said.lower()


# ------------------------------------------------------ the dead ends

def test_too_little_memory_names_the_numbers(failures):
    from relay import memory
    msg, (alert, alarm, log_line) = explained(failures,
        memory.InsufficientMemory({"available_mb": 10, "min_free_mb": 780}))
    assert "780" in msg.said
    assert alert == "not enough memory" and alarm is True
    assert log_line is None


def test_nothing_read_says_so(failures):
    from relay.site import driver as relay_site
    msg, (alert, alarm, log_line) = explained(failures, relay_site.NoStepsFound())
    assert "could not read" in msg.said.lower()
    assert alert == "the site read nothing this time" and alarm is True
    assert log_line is None


def test_a_changed_site_says_so_quietly(failures):
    """No alert popup: the user did nothing wrong, the page just moved."""
    from relay.site import driver as relay_site
    msg, (alert, alarm, log_line) = explained(failures,
        relay_site.SiteChanged("the upload form is gone"))
    assert "changed" in msg.said.lower()
    assert alert is None and alarm is False
    assert log_line is None


# ---------------------------------------------------------- the unknown

def test_an_unknown_failure_names_the_operation(failures):
    """The catch-all keeps each caller's headline: Upload, Sync, Commit."""
    for operation, headline in (("Upload", "Upload failed"),
                                ("Sync", "Sync failed"),
                                ("Commit", "Could not record")):
        msg, (alert, alarm, log_line) = explained(failures,
            RuntimeError("boom"), operation=operation)
        assert headline in msg.said, (operation, msg.said)
        assert alert is None and alarm is False


def test_an_unknown_failure_is_the_only_thing_logged(failures):
    """Routine failures are said out loud; the unknown one goes in the log."""
    _, (_, _, log_line) = explained(failures, RuntimeError("boom"), operation="Upload")
    assert log_line == "upload error: RuntimeError('boom')"


def test_the_log_line_uses_the_operation_name(failures):
    _, (_, _, sync_line) = explained(failures, RuntimeError("x"), operation="Sync")
    _, (_, _, commit_line) = explained(failures, RuntimeError("x"), operation="Commit")
    assert sync_line.startswith("sync error:")
    assert commit_line.startswith("commit error:")


def test_an_unknown_operation_falls_back_to_the_commit_copy(failures):
    """A new caller that forgets its headline still says something sane."""
    msg, _ = explained(failures, RuntimeError("boom"), operation="SomethingElse")
    assert "Could not record" in msg.said


def test_the_error_text_is_capped(failures):
    """A runaway exception message must not blow up the reply."""
    msg, _ = explained(failures, RuntimeError("x" * 500))
    assert len(msg.said) < 400
