"""Ledger: Submissions, the Credential Vault, and the overwrite guard's state."""
import pytest

from conftest import FAKE_TOKEN


# ---------------------------------------------------------------- submissions

def test_a_submission_round_trips(ledger):
    ledger.record("2026-10-04", 6532, reported="6,532", site_label="October 4th")
    row = ledger.last_submission("2026-10-04")
    assert row["steps"] == 6532
    assert row["reported"] == "6,532"


def test_one_row_per_activity_date(ledger):
    ledger.record("2026-10-04", 6532)
    ledger.record("2026-10-04", 6900)
    assert ledger.last_submission("2026-10-04")["steps"] == 6900
    assert len(ledger.all_submissions()) == 1


def test_submissions_come_back_newest_first(ledger):
    ledger.record("2026-10-01", 1000)
    ledger.record("2026-10-04", 6532)
    ledger.record("2026-10-03", 2831)
    assert [s["activity_date"] for s in ledger.all_submissions()] == [
        "2026-10-04", "2026-10-03", "2026-10-01"]


def test_a_missing_date_is_not_an_error(ledger):
    assert ledger.last_submission("1999-01-01") is None


# ---------------------------------------------------------------- credentials

def test_credentials_are_stored_per_chat(ledger):
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    ledger.save_credentials(2, "someone", "theirpass", FAKE_TOKEN)
    assert ledger.load_credentials(1, FAKE_TOKEN) == ("testuser", "testpass123")
    assert ledger.load_credentials(2, FAKE_TOKEN) == ("someone", "theirpass")


def test_the_stored_row_never_contains_the_password(ledger):
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    with ledger.conn() as c:
        raw = str([dict(r) for r in c.execute("SELECT * FROM credentials")])
    assert "testpass123" not in raw


def test_a_rotated_token_makes_them_unreadable(ledger):
    import vault
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    with pytest.raises(vault.DecryptionFailed):
        ledger.load_credentials(1, "999:ROTATEDTOKEN")


def test_preset_flag_is_recorded(ledger):
    ledger.save_credentials(1, "testuser", "p", FAKE_TOKEN, preset=True)
    assert ledger.credentials_stored(1)["preset"] == 1


def test_supplying_new_credentials_replaces_the_old(ledger):
    ledger.save_credentials(1, "testuser", "oldpass", FAKE_TOKEN)
    ledger.save_credentials(1, "testuser", "newpass", FAKE_TOKEN)
    assert ledger.load_credentials(1, FAKE_TOKEN) == ("testuser", "newpass")


def test_forget_removes_only_that_chat(ledger):
    ledger.save_credentials(1, "a", "p1", FAKE_TOKEN)
    ledger.save_credentials(2, "b", "p2", FAKE_TOKEN)
    ledger.forget_credentials(1)
    assert ledger.credentials_stored(1) is None
    assert ledger.credentials_stored(2) is not None


def test_forget_all_clears_the_vault(ledger):
    ledger.save_credentials(1, "a", "p1", FAKE_TOKEN)
    ledger.save_credentials(2, "b", "p2", FAKE_TOKEN)
    ledger.forget_all_credentials()
    assert ledger.all_submissions() == []   # unrelated data untouched
    assert ledger.credentials_stored(1) is None
    assert ledger.credentials_stored(2) is None


# -------------------------------------------------------------------- access

def test_grants_and_denials_are_separate_ledgers(ledger):
    ledger.grant(1, "claim")
    ledger.deny(2, "spam")
    assert ledger.has_access(1) == "claim"
    assert ledger.has_access(2) is None
    assert ledger.is_denied(2)
    assert not ledger.is_denied(1)


def test_granting_twice_does_not_duplicate(ledger):
    ledger.grant(1, "claim")
    ledger.grant(1, "secret:alice")
    assert len(ledger.all_access()) == 1
    assert ledger.has_access(1) == "secret:alice"


def test_telegram_users_are_remembered_once(ledger):
    ledger.remember_user(7, "casey")
    ledger.remember_user(7, "casey-renamed")
    assert len(ledger.known_users()) == 1


# -------------------------------------------------------- attempt throttling

def test_the_first_attempt_is_not_throttled(ledger):
    assert not ledger.secret_throttled(99)


def test_a_wrong_attempt_starts_the_clock(ledger):
    ledger.note_secret_attempt(99, ok=False)
    assert ledger.secret_throttled(99)


def test_a_correct_attempt_clears_the_clock(ledger):
    ledger.note_secret_attempt(99, ok=False)
    ledger.note_secret_attempt(99, ok=True)
    assert not ledger.secret_throttled(99)


def test_the_clock_is_per_chat(ledger):
    ledger.note_secret_attempt(1, ok=False)
    assert not ledger.secret_throttled(2)


def test_the_window_can_be_configured(ledger):
    ledger.note_secret_attempt(5, ok=False)
    assert not ledger.secret_throttled(5, window=0.0)
