"""Credentials Prompt state machine, and what /status is allowed to reveal.

The prompt is the piece that holds a password, so these tests are as much about
what is *not* leaked as about what works.
"""
import pytest

from conftest import FAKE_TOKEN


def _drive_username(bot, chat_id, username):
    bot.set_stage(chat_id, "username")
    bot.set_stage(chat_id, "password", username=username, preset=False)


# ------------------------------------------------------------------ staging

def test_a_chat_starts_with_no_prompt(bot):
    assert bot.prompt_stage(1) is None


def test_stages_advance_in_order(bot):
    bot.set_stage(1, "username")
    assert bot.prompt_stage(1)["stage"] == "username"
    _drive_username(bot, 1, "testuser")
    assert bot.prompt_stage(1)["stage"] == "password"


def test_the_username_survives_the_move_to_password(bot):
    _drive_username(bot, 1, "testuser")
    assert bot.prompt_stage(1)["username"] == "testuser"


def test_clearing_a_stage_forgets_it(bot):
    bot.set_stage(1, "username")
    bot.clear_stage(1)
    assert bot.prompt_stage(1) is None


def test_a_stale_prompt_is_forgotten(bot):
    bot.set_stage(1, "username")
    bot.PROMPTING[1]["at"] -= bot.PROMPT_TTL + 1
    assert bot.prompt_stage(1) is None


def test_a_fresh_prompt_survives(bot):
    bot.set_stage(1, "username")
    assert bot.prompt_stage(1) is not None


def test_prompts_are_per_chat(bot):
    bot.set_stage(1, "username")
    assert bot.prompt_stage(2) is None


# ------------------------------------------------------- credential presence

def test_no_credentials_until_they_are_supplied(bot):
    assert bot.has_credentials(1) is False


def test_stored_credentials_count(bot, ledger):
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    assert bot.has_credentials(1) is True


def test_a_preset_stage_needs_the_preset_to_still_exist(bot, monkeypatch):
    """Otherwise /status would claim to be ready and sign-in would fail later."""
    bot.set_stage(1, "ready", username="testuser", preset=True)
    assert bot.has_credentials(1) is False       # no preset configured

    monkeypatch.setenv("SITE_USERNAME", "testuser")
    monkeypatch.setenv("SITE_PASSWORD", "testpass123")
    from conftest import MODULES
    import sys
    for name in MODULES:
        sys.modules.pop(name, None)
    import relay.telegram as reloaded
    reloaded.set_stage(1, "ready", username="testuser", preset=True)
    assert reloaded.has_credentials(1) is True


# --------------------------------------------------- resolving what to use

def test_preset_credentials_are_used_when_chosen(bot, monkeypatch):
    monkeypatch.setenv("SITE_USERNAME", "testuser")
    monkeypatch.setenv("SITE_PASSWORD", "testpass123")
    from conftest import MODULES
    import sys
    for name in MODULES:
        sys.modules.pop(name, None)
    import relay.telegram as reloaded
    reloaded.set_stage(1, "ready", username="testuser", preset=True)
    assert reloaded.site_credentials(1) == ("testuser", "testpass123")


def test_stored_credentials_are_used_by_default(bot, ledger):
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    assert bot.site_credentials(1) == ("testuser", "testpass123")


def test_a_chat_with_no_credentials_raises(bot):
    """A named error, so the reply is 'send /login' rather than a traceback."""
    with pytest.raises(bot.NoCredentials):
        bot.site_credentials(1)


def test_a_rotated_token_surfaces_as_a_vault_failure(bot, ledger):
    """Rotating the token makes stored credentials unreadable, by design.

    site_credentials reads the token from config, so the test rotates config
    rather than passing a token: that is the real path.
    """
    from relay.store import vault
    ledger.save_credentials(1, "testuser", "testpass123", "111:OLDTOKEN")
    with pytest.raises(vault.DecryptionFailed):
        bot.site_credentials(1)      # config still holds FAKE_TOKEN


def test_a_rotated_token_is_distinguishable_from_a_missing_pair(bot, ledger):
    """They need different replies: re-prompt vs. ask for credentials."""
    from relay.store import vault
    ledger.save_credentials(1, "testuser", "testpass123", "111:OLDTOKEN")
    with pytest.raises(vault.DecryptionFailed):
        bot.site_credentials(1)
    ledger.forget_credentials(1)
    with pytest.raises(bot.NoCredentials):
        bot.site_credentials(1)


# ------------------------------------------------------------------ leakage

CANARY = "CANARY-MUST-NOT-LEAK-9f2b"


def test_no_reply_text_contains_the_stored_password(bot, ledger):
    """Sweep the whole copy table for the secret.

    Every user-facing string in words.py is called with a few argument shapes
    and none may contain a password. This is the check that would catch someone
    adding a debug line that interpolates the credential.
    """
    from relay.telegram import words
    ledger.save_credentials(1, "testuser", CANARY, FAKE_TOKEN)
    checked = 0
    for name in dir(words):
        if name.startswith("_"):
            continue
        fn = getattr(words, name)
        if not callable(fn):
            continue
        for args in ((), ("x",), ("x", "y"), (1,)):
            try:
                text = fn(*args)
            except Exception:
                continue
            checked += 1
            assert CANARY not in str(text), "%s leaked the password" % name
    assert checked > 20, "the sweep should reach a useful number of strings"


def test_the_ciphertext_does_not_contain_the_password(bot, ledger):
    ledger.save_credentials(1, "testuser", CANARY, FAKE_TOKEN)
    with ledger.conn() as c:
        rows = str([dict(r) for r in c.execute("SELECT * FROM credentials")])
    assert CANARY not in rows


def test_status_never_includes_a_password(bot, ledger):
    """The /status body is assembled from a fixed template, not from the vault."""
    ledger.save_credentials(1, "testuser", "SUPERSECRETVALUE", FAKE_TOKEN)
    stored = ledger.credentials_stored(1)
    # what /status is allowed to show about credentials
    assert stored["username"] == "testuser"
    assert "password" not in stored
    assert "SUPERSECRETVALUE" not in str(stored)


def test_a_failed_secret_attempt_is_recorded(bot, access):
    access.present_secret(1, "guess")
    assert access.ledger.secret_throttled(1)
