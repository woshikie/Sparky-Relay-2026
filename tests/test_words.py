"""Every user-facing string.

Not just smoke coverage: the point of these tests is that no reply can contain
a password, that refusals name the chat and the mode so the person can act, and
that the warnings say the numbers involved rather than gesturing at them.
"""
import pytest

from conftest import FAKE_TOKEN


@pytest.fixture
def words(bot):
    return bot.words


def render(words, name, *args):
    return str(getattr(words, name)(*args))


# --------------------------------------------------------------- no leakage

def test_no_string_contains_a_password(words, ledger):
    ledger.save_credentials(1, "testuser", "CANARY-SECRET-1234", FAKE_TOKEN)
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
            assert "CANARY-SECRET-1234" not in str(text), name
    assert checked > 20


def test_nothing_echoes_a_preset_password(words, bot, monkeypatch):
    """Even the preset is only ever named by username."""
    monkeypatch.setenv("SITE_USERNAME", "testuser")
    monkeypatch.setenv("SITE_PASSWORD", "PRESETPASS-SECRET")
    import sys
    from conftest import MODULES
    for m in MODULES:
        sys.modules.pop(m, None)
    import relay.telegram as reloaded
    w = reloaded.words
    for name in ("choose_preset", "using_preset", "credentials_saved"):
        assert "PRESETPASS-SECRET" not in str(getattr(w, name)("testuser"))


# ---------------------------------------------------------------- access

def _refuse(monkeypatch, tmp_path, **env):
    """A Decision from a real access.check, in a given mode."""
    from conftest import reload_with
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path), **env)
    from relay.telegram import access
    from relay.store import ledger
    ledger.init()
    return access, access.check(4242)


def test_a_denial_names_the_chat_and_the_fix(monkeypatch, tmp_path, bot):
    acc, d = _refuse(monkeypatch, tmp_path, ACCESS_MODE="blacklist")
    acc.deny(4242)
    d = acc.check(4242)
    text = bot.words.access_refused(d)
    assert "4242" in text
    assert "DENY_CHAT_IDS" in text


def test_a_claim_need_tells_you_it_was_taken(monkeypatch, tmp_path, bot):
    _, d = _refuse(monkeypatch, tmp_path, ACCESS_MODE="whitelist_claim")
    assert "whitelist_claim" in bot.words.access_refused(d)


def test_a_secret_need_shows_the_prompt(monkeypatch, tmp_path, bot):
    _, d = _refuse(monkeypatch, tmp_path, ACCESS_MODE="shared_secret")
    assert "Shared Secret" in bot.words.access_refused(d)


def test_a_throttled_chat_is_told_to_wait(monkeypatch, tmp_path, bot):
    acc, d = _refuse(monkeypatch, tmp_path, ACCESS_MODE="shared_secret",
                     SHARED_SECRETS="a:one")
    acc.ledger.init_secrets_from_env("a:one")
    acc.present_secret(4242, "wrong")
    d = acc.check(4242)
    assert d.why == "secret_throttled"
    assert "lockout" in bot.words.access_refused(d)


def test_claiming_announces_ownership(words):
    assert "owns the Relay" in words.claimed()


def test_claiming_twice_explains_why_not(words):
    text = words.already_claimed()
    assert "already" in text
    assert "ACCESS_MODE" in text


def test_the_secret_prompt_warns_about_notifications(words):
    text = words.secret_prompt()
    assert "delete" in text
    assert "notification" in text
    assert "private chat" in text


def test_a_wrong_secret_does_not_say_which_part_was_wrong(words):
    """A hint here would turn the rate limit into a wordlist oracle."""
    text = words.secret_rejected()
    assert "not a valid" in text
    assert "character" not in text.lower()
    assert "starts with" not in text.lower()


def test_throttling_is_announced_as_not_a_lockout(words):
    text = words.secret_throttled(30)
    assert "30" in text
    assert "not a lockout" in text
    assert "second" in text


def test_throttling_is_singular_for_one_second(words):
    assert "1 second." in words.secret_throttled(1)


def test_accepted_records_which_secret(words):
    assert "alice" in words.secret_accepted("secret:alice")


# ------------------------------------------------------------- credentials

def test_the_preset_choice_names_the_account_not_the_password(words):
    text = words.choose_preset("testuser")
    assert "testuser" in text
    assert "never shown" in text


def test_a_bad_username_says_the_rule(words):
    text = words.bad_username()
    assert "3" in text and "40" in text


def test_the_password_prompt_explains_deletion_and_encryption(words):
    text = words.ask_password()
    assert "delete" in text
    assert "encrypted" in text


def test_saving_mentions_token_rotation(words):
    text = words.credentials_saved("testuser")
    assert "testuser" in text
    assert "BotFather" in text


def test_a_store_failure_says_nothing_was_saved(words):
    assert "Nothing was stored" in words.credential_store_failed("disk full")


def test_an_unreadable_vault_blames_token_rotation(words):
    """Distinct from a wrong password: the fix is different."""
    text = words.vault_unreadable("wrong key")
    assert "rotated" in text
    assert "/login" in text


def test_need_credentials_explains_why_the_browser_is_required(words):
    text = words.need_credentials()
    assert "/login" in text
    assert "OCR" in text


def test_a_failedscrub_asks_the_user_to_delete(words):
    assert "delete it yourself" in words.scrub_failed()


def test_logout_confirms_it_forgot(words):
    assert "forgotten" in words.logged_out()


def test_nothing_to_do_suggests_the_next_step(words):
    assert "/login" in words.no_prompt()


# ----------------------------------------------------- submission outcomes

def test_the_low_memory_message_is_not_a_failure(words):
    """It must read as 'try again', not as 'something broke'."""
    text = words.low_memory()
    assert "not recorded" in text
    assert "/login" not in text


def test_the_low_memory_message_can_carry_the_detail(words):
    assert "need 780MB" in words.low_memory("need 780MB free")


def test_no_steps_explains_what_a_good_screenshot_looks_like(words):
    text = words.no_steps()
    assert "day view" in text
    assert "not recorded" in text or "not recorded anything" in text


def test_an_implausible_read_is_never_recorded(words):
    text = words.implausible("3")
    assert "plausible" in text
    assert "stopped" in text


def test_a_site_change_stops_instead_of_guessing(words):
    text = words.site_changed("no date button")
    assert "no date button" in text
    assert "nothing was recorded" in text.lower()


def test_a_changed_second_read_stops_the_submission(words):
    """The user confirmed one number; a different one must not be submitted."""
    text = words.ocr_changed("6,532", "700", "October 4th, 2026")
    assert "6,532" in text
    assert "700" in text
    assert "Nothing was saved" in text


def test_recording_confirms_with_both_numbers(words):
    text = words.recorded("6,532", "October 4th, 2026", "2026-10-04")
    assert "6,532" in text
    assert "October 4th, 2026" in text


def test_cancelling_is_acknowledged(words):
    assert "Nothing was recorded" in words.cancelled()


def test_the_date_question_shows_the_number(words):
    assert "6,532" in words.choose_date(6532, "6,532", "")


def test_the_downgrade_warning_states_the_difference(words):
    text = words.overwrite_warning(700, 6532, "October 4th, 2026")
    assert "5,832" in text
    assert "blurry" in text


# ---------------------------------------------------------------- reporting

def test_the_log_is_empty_when_there_is_nothing(words):
    assert "No Submissions" in words.log_lines([])


def test_the_log_lists_what_is_recorded(words):
    subs = [{"activity_date": "2026-10-04", "steps": 6532,
             "recorded_at": "2026-10-05T21:00:00"}]
    text = words.log_lines(subs)
    assert "2026-10-04" in text
    assert "6,532" in text


def test_the_log_is_capped(words):
    """More than a fortnight of history must not become a wall of text."""
    subs = [{"activity_date": "2026-%02d-%02d" % (d % 12 + 1, d % 28 + 1),
             "steps": 1000, "recorded_at": "2026-10-05T21:00:00"}
            for d in range(40)]
    text = words.log_lines(subs)
    # One backticked date per row; the recorded_at stamps are not rows.
    assert text.count("`2026-") <= 15


def test_a_rank_line_reports_what_it_has(words):
    line = words.rank_line(11225, 10, "Esplanade")
    assert "Esplanade" in line
    assert "11,225" in line
    assert "10" in line


def test_a_rank_line_can_omit_the_house(words):
    assert words.rank_line(100, 1) != ""


# --------------------------------------------------- the uncovered branches

def test_access_refused_for_a_blacklist_denial(words):
    """A refusal has to be actionable: which mode, and which chat."""
    class D:
        why = "denied"
    out = words.access_refused(D())
    assert "deny list" in out and "Chat id" in out


def test_access_refused_for_a_throttled_secret(words):
    """The rate limit is not a lockout, and the copy has to say so."""
    class D:
        why = "secret_throttled"
        retry_after = 30
    out = words.access_refused(D())
    assert "rate limit" in out and "30" in out


def test_access_refused_when_a_secret_is_needed(words):
    class D:
        why = "needs_secret"
    assert "secret" in words.access_refused(D()).lower()


def test_access_refused_when_someone_else_claimed(words):
    class D:
        why = "needs_claim"
    assert words.access_refused(D())


def test_access_refused_for_a_reason_with_no_specific_copy(words):
    """The fallback: still names the mode and the chat."""
    class D:
        why = "something_else"
    out = words.access_refused(D())
    assert "Access Mode" in out and "Chat id" in out


def test_already_claimed_says_so(words):
    assert words.already_claimed()


def test_secret_prompt_asks_for_the_secret(words):
    assert "secret" in words.secret_prompt().lower()


def test_implausible_names_the_number_it_refused(words):
    """The number is the thing the user needs to see to understand."""
    out = words.implausible("999,999")
    assert "999,999" in out


def test_ocr_read_when_the_number_is_implausible(words):
    """The band exists to stop a bad read being recorded as fact."""
    out = words.ocr_read(999999, "999,999", plausible=False)
    assert "outside the range" in out


def test_recorded_quotes_the_sites_own_line(words):
    """After Commit the site's own words are the evidence, not ours."""
    out = words.recorded("4,272", "October 5th, 2026", "2026-10-05",
                         "Recorded 4,272 steps for 5 Oct 2026")
    assert "Recorded 4,272 steps" in out


def test_recorded_without_site_text_still_confirms(words):
    assert "Recorded" in words.recorded("4,272", "October 5th, 2026",
                                        "2026-10-05")


def test_recorded_ignores_a_site_line_with_no_numbers(words):
    """A blank or irrelevant line must not be quoted as evidence."""
    out = words.recorded("4,272", "October 5th, 2026", "2026-10-05",
                         "Welcome back")
    assert ">" not in out


def test_rank_line_with_everything(words):
    out = words.rank_line(24860, 248, house="24,860", position="3rd")
    assert "House" in out and "24,860" in out
    assert "You" in out and "24,860" in out
    assert "3rd" in out
    assert "248" in out


def test_rank_line_with_only_a_total(words):
    """The common case: a total and nothing else to say about rank."""
    out = words.rank_line(24860, None)
    assert "24,860" in out
    assert "House" not in out


def test_rank_line_with_no_total_still_reports_points(words):
    out = words.rank_line(None, 248)
    assert "248" in out
    assert "You" not in out


def test_rank_line_with_nothing_at_all(words):
    """Degenerate, but it must not raise."""
    assert words.rank_line(None, None)


def test_recorded_skips_a_blank_line_in_the_site_text(words):
    """A blank line must be skipped, not quoted."""
    out = words.recorded("4,272", "October 5th, 2026", "2026-10-05",
                         "\nRecorded 4,272 steps for 5 Oct 2026")
    assert "Recorded 4,272 steps" in out
