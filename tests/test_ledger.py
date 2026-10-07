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


def test_the_stored_row_never_contains_the_password(ledger, db):
    ledger.save_credentials(1, "testuser", "testpass123", FAKE_TOKEN)
    with db.conn() as c:
        raw = str([dict(r) for r in c.execute("SELECT * FROM credentials")])
    assert "testpass123" not in raw


def test_a_rotated_token_makes_them_unreadable(ledger):
    from relay.store import vault
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

def test_grants_and_denials_are_separate_ledgers(ledger, policy):
    policy.grant(1, "claim")
    policy.deny(2, "spam")
    assert policy.has_access(1) == "claim"
    assert policy.has_access(2) is None
    assert policy.is_denied(2)
    assert not policy.is_denied(1)


def test_granting_twice_does_not_duplicate(ledger, policy):
    policy.grant(1, "claim")
    policy.grant(1, "secret:alice")
    assert len(policy.all_access()) == 1
    assert policy.has_access(1) == "secret:alice"


def test_telegram_users_are_remembered_once(ledger, policy):
    policy.remember_user(7, "casey")
    policy.remember_user(7, "casey-renamed")
    assert len(policy.known_users()) == 1


# -------------------------------------------------------- attempt throttling

def test_the_first_attempt_is_not_throttled(ledger, policy):
    assert not policy.secret_throttled(99)


def test_a_wrong_attempt_starts_the_clock(ledger, policy):
    policy.note_secret_attempt(99, ok=False)
    assert policy.secret_throttled(99)


def test_a_correct_attempt_clears_the_clock(ledger, policy):
    policy.note_secret_attempt(99, ok=False)
    policy.note_secret_attempt(99, ok=True)
    assert not policy.secret_throttled(99)


def test_the_clock_is_per_chat(ledger, policy):
    policy.note_secret_attempt(1, ok=False)
    assert not policy.secret_throttled(2)


def test_the_window_can_be_configured(ledger, policy):
    policy.note_secret_attempt(5, ok=False)
    assert not policy.secret_throttled(5, window=0.0)


# ------------------------------------------- the secret-spec parsing branches

def test_a_blank_line_in_the_spec_is_skipped(ledger, policy):
    labels = policy.sync_secrets_from_env("a:one\n\nb:two")
    assert sorted(labels) == ["a", "b"]


def test_a_comment_line_in_the_spec_is_skipped(ledger, policy):
    labels = policy.sync_secrets_from_env("# a comment\na:one")
    assert labels == ["a"]


def test_a_line_with_no_colon_derives_its_label(ledger, policy):
    """The label is what revocation is keyed on, so it must not print the secret."""
    labels = policy.sync_secrets_from_env("just-a-secret")
    assert labels == ["secret-just-a-s"]
    assert "just-a-secret" not in labels[0]


def test_a_line_with_an_empty_secret_is_skipped(ledger, policy):
    """'label:' with nothing after it is not a secret."""
    labels = policy.sync_secrets_from_env("a:one\nb:")
    assert labels == ["a"]


def test_a_spec_of_only_blank_lines_loads_nothing(ledger, policy):
    assert policy.sync_secrets_from_env("\n\n") == []


def test_a_spec_that_is_none_loads_nothing(ledger, policy):
    """An operator adding secrets with /addsecret must not be clobbered."""
    policy.add_secret("keep", "value")
    assert policy.sync_secrets_from_env(None) == []
    assert policy.list_secret_labels() == ["keep"]


def test_sync_with_an_empty_spec_preserves_secrets(ledger, policy):
    """An empty spec parses to no entries, so nothing is replaced."""
    policy.add_secret("keep", "value")
    assert policy.sync_secrets_from_env("") == []
    assert policy.check_secret("value") == "keep"
    assert policy.sync_secrets_from_env("# only a comment") == []
    assert policy.check_secret("value") == "keep"
    assert policy.list_secret_labels() == ["keep"]


def test_list_secret_labels_with_no_spec_returns_nothing(ledger, policy):
    assert policy.list_secret_labels() == []


# ------------------------------------------------------- init() itself

def test_init_with_a_bare_filename_creates_no_directory(ledger, monkeypatch, tmp_path, db):
    """DB with no directory component: dirname is '', and '' is not a path.

    A relative filename is the case where dirname is empty, so the makedirs
    branch is skipped. Run inside tmp_path so the file lands there.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DB", "ledger.sqlite3")
    assert db.init() == "ledger.sqlite3"
    assert (tmp_path / "ledger.sqlite3").exists()


def test_init_secrets_with_a_blank_spec_loads_nothing(ledger, policy):
    """A spec of blank lines parses to no entries, so nothing is replaced."""
    policy.add_secret("keep", "value")
    loaded, total = policy.init_secrets_from_env("\n# comment\n")
    assert (loaded, total) == (False, 0)
    assert policy.list_secret_labels() == ["keep"]


def test_init_secrets_with_a_bare_secret_derives_the_label(ledger, policy):
    loaded, total = policy.init_secrets_from_env("just-a-secret")
    assert (loaded, total) == (True, 1)
    assert policy.list_secret_labels() == ["secret-just-a-s"]


def test_init_secrets_skips_a_line_with_an_empty_secret(ledger, policy):
    """:'label:' with nothing after it is not a secret."""
    loaded, total = policy.init_secrets_from_env("a:one\nb:")
    assert (loaded, total) == (True, 1)
    assert policy.list_secret_labels() == ["a"]


def test_init_creates_the_directory_when_there_is_one(ledger, monkeypatch, tmp_path, db):
    nested = tmp_path / "a" / "b" / "ledger.sqlite3"
    monkeypatch.setattr(db, "DB", str(nested))
    db.init()
    assert nested.parent.is_dir()
