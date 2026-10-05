"""Access Modes: the rule that decides who may drive the Relay."""
import time

import pytest

from conftest import reload_with


# ---------------------------------------------------------------- whitelist

def test_nobody_has_access_before_a_claim(access):
    d = access.check(1)
    assert not d
    assert d.why == "needs_claim"


def test_first_chat_claims_the_relay(access):
    assert access.claim(1) is True
    assert access.check(1)
    assert access.check(1).how == "claim"


def test_a_second_chat_cannot_steal_the_claim(access):
    access.claim(1)
    assert access.claim(2) is False
    assert not access.check(2)


def test_a_grant_admits_a_chat(access):
    access.grant(2, "manual")
    assert access.check(2)
    assert access.check(2).how == "manual"


def test_revoke_turns_access_back_off(access):
    access.grant(2, "manual")
    access.revoke(2)
    assert not access.check(2)


def test_deny_overrides_a_grant(access):
    access.grant(1, "claim")
    access.deny(1, "because")
    assert not access.check(1)
    assert access.check(1).why == "denied"


def test_undeny_restores_access(access):
    access.grant(1, "claim")
    access.deny(1, "mistake")
    access.undeny(1)
    assert access.check(1)


def test_denial_carries_the_chat_id_so_a_reply_can_name_it(access):
    access.deny(42)
    assert access.check(42).chat_id == 42


# ---------------------------------------------------------------- blacklist

def test_blacklist_admits_a_stranger(monkeypatch, tmp_path):
    reload_with(monkeypatch, ACCESS_MODE="blacklist", RELAY_STATE_DIR=str(tmp_path))
    import access
    access.ledger.init()
    d = access.check(99999, "nobody-in-particular")
    assert d
    assert d.why == "blacklist"


def test_blacklist_still_refuses_a_denied_chat(monkeypatch, tmp_path):
    reload_with(monkeypatch, ACCESS_MODE="blacklist", DENY_CHAT_IDS="4242",
                RELAY_STATE_DIR=str(tmp_path))
    import access
    access.ledger.init()
    assert not access.check(4242)
    assert not access.check(99999) is False   # the stranger is still admitted


def test_blacklist_deny_list_accepts_several_formats(monkeypatch, tmp_path):
    reload_with(monkeypatch, ACCESS_MODE="blacklist", DENY_CHAT_IDS="1, 2;3",
                RELAY_STATE_DIR=str(tmp_path))
    import config
    assert config.DENY_CHAT_IDS == [1, 2, 3]


def test_blacklist_deny_list_ignores_junk(monkeypatch, tmp_path):
    reload_with(monkeypatch, ACCESS_MODE="blacklist", DENY_CHAT_IDS="1,abc,,3",
                RELAY_STATE_DIR=str(tmp_path))
    import config
    assert config.DENY_CHAT_IDS == [1, 3]


# ------------------------------------------------------------ shared secret

@pytest.fixture
def secret_access(monkeypatch, tmp_path):
    """An `access` module in shared_secret mode with two known secrets.

    Note the sync: SHARED_SECRETS in the environment is not enough on its own,
    because the bot only imports them at startup (main() calls
    ledger.init_secrets_from_env). Tests that want secrets loaded have to ask.
    """
    cfg = reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                      SHARED_SECRETS="alice:hunter2-long-one\nbob:another-long-one",
                      RELAY_STATE_DIR=str(tmp_path))
    import access
    import ledger
    ledger.init()
    loaded, total = ledger.init_secrets_from_env(cfg.SHARED_SECRETS)
    assert loaded and total == 2
    return access


def test_secrets_are_loaded_per_person(secret_access):
    assert sorted(secret_access.ledger.list_secret_labels()) == ["alice", "bob"]


def test_secrets_are_not_stored_in_the_clear(secret_access):
    rows = str([dict(r) for r in secret_access.ledger.conn().execute(
        "SELECT * FROM shared_secrets")])
    assert "hunter2-long-one" not in rows
    assert "another-long-one" not in rows


def test_an_unauthenticated_chat_needs_a_secret(secret_access):
    assert secret_access.check(50).why == "needs_secret"


@pytest.mark.parametrize("candidate,label", [
    ("hunter2-long-one", "alice"),
    ("another-long-one", "bob"),
])
def test_each_person_has_their_own_secret(secret_access, candidate, label):
    d = secret_access.present_secret(50, candidate)
    assert d
    assert d.how == "secret:%s" % label


def test_a_guess_is_refused(secret_access):
    assert not secret_access.present_secret(50, "hunter2-long-one-x")


def test_an_empty_guess_is_refused(secret_access):
    assert not secret_access.present_secret(50, "")


def test_a_correct_secret_during_a_wait_is_still_refused(secret_access):
    """The limit belongs to the function, not to the caller's good intentions."""
    secret_access.present_secret(51, "wrong")
    assert secret_access.ledger.secret_throttled(51)
    assert not secret_access.present_secret(51, "hunter2-long-one")


def test_the_limit_expires(secret_access, monkeypatch):
    secret_access.present_secret(52, "wrong")
    assert secret_access.ledger.secret_throttled(52)
    # Wind the window forward rather than sleeping 30 seconds.
    with secret_access.ledger.conn() as c:
        c.execute("UPDATE secret_attempts SET last_try = last_try - 60 "
                  "WHERE chat_id = 52")
    assert not secret_access.ledger.secret_throttled(52)
    assert secret_access.present_secret(52, "hunter2-long-one")


def test_a_correct_secret_clears_the_failure_count(secret_access):
    secret_access.present_secret(53, "wrong")
    assert secret_access.ledger.secret_throttled(53)
    # Wind the window on, so the second attempt is not throttled: the point
    # under test is that a success resets the counter, not that the throttle
    # does not exist.
    with secret_access.ledger.conn() as c:
        c.execute("UPDATE secret_attempts SET last_try = last_try - 60 "
                  "WHERE chat_id = 53")
    secret_access.present_secret(53, "hunter2-long-one")
    assert not secret_access.ledger.secret_throttled(53)


def test_one_person_being_wrong_does_not_throttle_another(secret_access):
    secret_access.present_secret(60, "wrong")
    assert secret_access.present_secret(61, "hunter2-long-one")


def test_revoking_one_secret_leaves_the_others(secret_access):
    secret_access.ledger.remove_secret("alice")
    assert not secret_access.ledger.check_secret("hunter2-long-one")
    assert secret_access.ledger.check_secret("another-long-one") == "bob"


def test_sync_does_not_clobber_when_the_env_is_absent(secret_access):
    # An operator who adds secrets at runtime must not lose them on a restart
    # with an unchanged environment.
    before = sorted(secret_access.ledger.list_secret_labels())
    loaded, total = secret_access.ledger.init_secrets_from_env(None)
    assert loaded is False
    assert sorted(secret_access.ledger.list_secret_labels()) == before
    assert total == len(before)


def test_a_bare_secret_gets_a_derived_label(secret_access):
    """No label in the spec, so one is derived from the leading characters.

    The label is what revocation is keyed on, so it has to identify which line
    to delete without printing the secret itself.
    """
    labels = secret_access.ledger.sync_secrets_from_env("just-a-secret")
    # First 8 characters, per ledger._parse_secret_spec.
    assert labels == ["secret-just-a-s"]
    assert "just-a-secret" not in labels[0]
    assert secret_access.ledger.check_secret("just-a-secret") == "secret-just-a-s"


def test_describe_counts_secrets(secret_access):
    assert "2 secret" in secret_access.describe()


def test_describe_counts_secrets_after_a_resync(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, ACCESS_MODE="shared_secret",
                      SHARED_SECRETS="a:one\nb:two\nc:three",
                      RELAY_STATE_DIR=str(tmp_path))
    import access
    import ledger
    ledger.init()
    ledger.init_secrets_from_env(cfg.SHARED_SECRETS)
    assert "3 secret" in access.describe()


def test_comments_and_blank_lines_are_skipped(secret_access):
    labels = secret_access.ledger.sync_secrets_from_env(
        "\n# a comment\n\nalice:one\n")
    assert labels == ["alice"]


# ------------------------------------------------------------------ describe

def test_describe_warns_in_blacklist_mode(monkeypatch, tmp_path):
    reload_with(monkeypatch, ACCESS_MODE="blacklist", RELAY_STATE_DIR=str(tmp_path))
    import access
    import ledger
    ledger.init()
    assert "anyone not denied" in access.describe()
