"""The dashboard parsers, and the read-back path.

The parsers are module-level functions rather than methods on Relay, because
they are the part most likely to break and the part that must be testable
without a browser. A regex that stops matching the site's markup fails silently
in production -- read_days() just returns fewer rows -- so it gets pinned here
against the real strings.

The DOM this is written against, from the live dashboard:

    <ul class="space-y-2">
      <li class="surface-card flex items-center justify-between p-3 text-sm">
        <div class="min-w-0 flex-1">
          <p class="font-semibold text-foreground">5 Oct 2026</p>
          <p class="text-xs text-muted-foreground">Daily steps</p>
        </div>
        <span class="ml-3 shrink-0 rounded-full bg-secondary px-2.5 py-1
              text-xs font-semibold text-secondary-foreground">+4,272 steps</span>
      </li>
    </ul>
"""
import datetime
import importlib
import types

import pytest


@pytest.fixture
def bot():
    return importlib.import_module("relay.telegram")


@pytest.fixture
def relay_site():
    """Not `from relay.site import driver` at the top: _purge() makes stale."""
    return importlib.import_module("relay.site.driver")


@pytest.fixture
def parsing():
    """The pure functions, apart from the browser that uses them."""
    return importlib.import_module("relay.site.parsing")


# ------------------------------------------------------------ parse_day

@pytest.mark.parametrize("text, want", [
    ("5 Oct 2026", datetime.date(2026, 10, 5)),
    ("31 Oct 2026", datetime.date(2026, 10, 31)),
    ("1 Jan 2026", datetime.date(2026, 1, 1)),
    ("29 Feb 2028", datetime.date(2028, 2, 29)),   # a leap day
    ("15 December 2026", datetime.date(2026, 12, 15)),  # full month name
])
def test_a_date_parses(parsing, text, want):
    assert parsing.parse_day(text) == want


@pytest.mark.parametrize("text", [
    "", " ", "Oct 2026", "2026-10-05", "05/10/2026",
    "32 Oct 2026",          # no such day
    "29 Feb 2026",          # not a leap year
    "5 Oct",                # no year
    "Daily steps",          # the other <p> in the same <li>
    "5 Oct 2026 (today)",
])
def test_a_date_that_does_not_fit_returns_none(parsing, text):
    assert parsing.parse_day(text) is None, text


def test_a_garbage_month_returns_none(parsing):
    assert parsing.parse_day("5 Xyz 2026") is None


# ---------------------------------------------------------- parse_steps

@pytest.mark.parametrize("text, want", [
    ("+4,272 steps", 4272),
    ("+1 step", 1),
    ("+200,000 steps", 200000),
    ("4,272 steps", 4272),          # no plus
    ("+4272 steps", 4272),          # no comma
    ("+4,272 STEPS", 4272),         # case
])
def test_a_step_count_parses(parsing, text, want):
    assert parsing.parse_steps(text) == want


@pytest.mark.parametrize("text", [
    "", " ", "steps", "+ steps", "4,272", "4.272 steps",
    "5 Oct 2026",                   # the date <p>, not the badge
    "Daily steps",
    "24,860",                       # the total, which is not a day
])
def test_a_step_count_that_does_not_fit_returns_none(parsing, text):
    assert parsing.parse_steps(text) is None, text


def test_the_two_parsers_agree_on_a_real_row(parsing):
    """The pair that matters: a date and a badge from the same <li>."""
    day = parsing.parse_day("5 Oct 2026")
    steps = parsing.parse_steps("+4,272 steps")
    assert (day, steps) == (datetime.date(2026, 10, 5), 4272)


# ------------------------------------------------------- read_days itself

class FakeRelay:
    """Stands in for Relay so read_days can be tested without a browser.

    The point is the loop over <li> elements and the skip of rows that do not
    parse, not the navigation. The real read_days is bound on, so what is
    under test is the parsing and the skipping -- not a copy of it.
    """

    def __init__(self, rows, relay_site):
        self._rows = rows
        self.base = "https://example.test"
        self.driver = self
        self.log = []
        self.read_days = types.MethodType(relay_site.Relay.read_days, self)

    # -- what read_days touches on the driver --
    def get(self, url):
        pass

    def _text(self, el):
        """The real one tolerates a detached node; a fake has no such problem."""
        try:
            return (el.text or "").strip()
        except Exception:
            return None

    def find_elements(self, by, selector):
        if selector == "a":
            return []
        if selector == "ul.space-y-2 > li":
            # _rows holds either (date, steps) tuples or ready-made FakeLi
            # objects, so a test can substitute a row with odd behaviour.
            out = []
            for item in self._rows:
                if isinstance(item, FakeLi):
                    out.append(item)
                else:
                    d, s = item
                    out.append(FakeLi(d, s))
            return out
        if selector == "p":
            return []
        if selector == "span":
            return []
        return []

    def _body(self):
        return "House standings"


class FakeLi:
    # The third argument is accepted and ignored: the subclasses below are
    # constructed the same way as FakeLi, and only FakeRelay needs it.
    def __init__(self, date_text, steps_text, _relay_site=None):
        self._p = [FakeEl(date_text), FakeEl("Daily steps")]
        self._span = [FakeEl(steps_text)]

    def find_elements(self, by, selector):
        if selector == "p":
            return self._p
        if selector == "span":
            return self._span
        return []


class FakeEl:
    def __init__(self, text):
        self.text = text


def test_read_days_returns_date_and_steps(relay_site):
    r = FakeRelay([("5 Oct 2026", "+4,272 steps"),
                   ("4 Oct 2026", "+6,532 steps")], relay_site)
    assert r.read_days() == [(datetime.date(2026, 10, 5), 4272),
                             (datetime.date(2026, 10, 4), 6532)]


def test_read_days_skips_a_row_it_cannot_parse(relay_site):
    """One bad row must not fail the whole sync."""
    r = FakeRelay([("5 Oct 2026", "+4,272 steps"),
                   ("not a date", "+1 steps"),
                   ("4 Oct 2026", "+6,532 steps")], relay_site)
    assert r.read_days() == [(datetime.date(2026, 10, 5), 4272),
                             (datetime.date(2026, 10, 4), 6532)]


def test_read_days_skips_a_row_with_no_badge(relay_site):
    r = FakeRelay([("5 Oct 2026", "+4,272 steps"),
                   ("4 Oct 2026", ""),
                   ("3 Oct 2026", "+2,831 steps")], relay_site)
    assert r.read_days() == [(datetime.date(2026, 10, 5), 4272),
                             (datetime.date(2026, 10, 3), 2831)]


def test_read_days_on_an_empty_list_returns_nothing(relay_site):
    assert FakeRelay([], relay_site).read_days() == []


def test_read_days_ignores_a_row_with_no_paragraphs(relay_site):
    class NoP(FakeLi):
        def find_elements(self, by, selector):
            return [] if selector == "p" else super().find_elements(by, selector)
    r = FakeRelay([("5 Oct 2026", "+4,272 steps")], relay_site)
    r._rows = [NoP("5 Oct 2026", "+4,272 steps", relay_site)]
    assert r.read_days() == []


def test_read_days_uses_the_last_span(relay_site):
    """The badge is the last <span>; earlier ones are the 'Daily steps' label."""
    class TwoSpans(FakeLi):
        def find_elements(self, by, selector):
            if selector == "span":
                return [FakeEl("Daily steps"), FakeEl("+4,272 steps")]
            return super().find_elements(by, selector)
    r = FakeRelay([("5 Oct 2026", "+4,272 steps")], relay_site)
    r._rows = [TwoSpans("5 Oct 2026", "+4,272 steps", relay_site)]
    assert r.read_days() == [(datetime.date(2026, 10, 5), 4272)]


# ------------------------------------------------------- the bot wiring

def test_the_sync_button_is_on_the_keyboard(bot):
    labels = [b.text for row in bot.kb_reply().keyboard for b in row]
    assert any("Sync" in l for l in labels), labels


def test_the_sync_button_has_a_handler(bot):
    assert bot.LABEL_SYNC in bot.button_actions()
    assert bot.button_actions()[bot.LABEL_SYNC] == "sync"


def test_the_sync_command_is_registered(bot):
    import inspect
    import re
    patterns = re.findall(r'CommandHandler\("(\w+)"',
                          inspect.getsource(bot.main))
    assert "sync" in patterns, patterns


def test_the_overwrite_guard_asks_the_site_first(bot, ledger):
    """A day the user entered by hand must count, or the guard is blind."""
    ledger.record_site_days([(datetime.date(2026, 10, 1), 5829)])
    got = ledger.current_value("2026-10-01")
    assert got["steps"] == 5829


def test_the_guard_falls_back_to_the_bots_own_record(bot, ledger):
    ledger.record("2026-10-03", 2831, "2,831", "October 3rd, 2026", 52)
    got = ledger.current_value("2026-10-03")
    assert got["steps"] == 2831


def test_the_guard_prefers_the_site_when_they_disagree(bot, ledger):
    """The site is the authority, not our memory of what we wrote."""
    ledger.record("2026-10-03", 2831, "2,831", "October 3rd, 2026", 52)
    ledger.record_site_days([(datetime.date(2026, 10, 3), 3000)])
    assert ledger.current_value("2026-10-03")["steps"] == 3000


def test_the_guard_on_an_unknown_day_returns_nothing(bot, ledger):
    assert ledger.current_value("2026-09-30") is None


# ------------------------------------------------------------ the ledger

def test_record_site_days_writes_what_the_site_says(bot, ledger):
    n = ledger.record_site_days([(datetime.date(2026, 10, 1), 5829),
                                 (datetime.date(2026, 10, 2), 5396)])
    assert n == 2
    assert ledger.site_value("2026-10-01")["steps"] == 5829
    assert ledger.site_value("2026-10-02")["steps"] == 5396


def test_record_site_days_reports_the_count(bot, ledger):
    assert ledger.record_site_days([]) == 0


def test_re_syncing_refreshes_rather_than_duplicates(bot, ledger):
    ledger.record_site_days([(datetime.date(2026, 10, 1), 5829)])
    ledger.record_site_days([(datetime.date(2026, 10, 1), 6000)])
    rows = ledger.all_site_days()
    assert len(rows) == 1
    assert rows[0]["steps"] == 6000


def test_record_site_days_accepts_iso_strings(bot, ledger):
    """read_days returns dates, but a caller may hand over strings."""
    ledger.record_site_days([("2026-10-01", 5829)])
    assert ledger.site_value("2026-10-01")["steps"] == 5829


def test_site_days_are_separate_from_submissions(bot, ledger):
    """/log reports what the bot wrote. A sync is not a submission."""
    ledger.record_site_days([(datetime.date(2026, 10, 1), 5829)])
    assert ledger.all_submissions() == []
    assert len(ledger.all_site_days()) == 1


def test_the_reported_column_is_formatted(bot, ledger):
    ledger.record_site_days([(datetime.date(2026, 10, 1), 5829)])
    assert ledger.site_value("2026-10-01")["reported"] == "5,829"


# ------------------------------------------------------------------ copy

def test_the_sync_report_states_the_count(bot):
    from relay.telegram import words
    days = [(datetime.date(2026, 10, 5), 4272),
            (datetime.date(2026, 10, 4), 6532)]
    out = words.sync_report(days, 2)
    assert "Synced 2 days" in out
    assert "4,272" in out and "6,532" in out


def test_the_sync_report_says_it_submitted_nothing(bot):
    """The whole point of the button; the copy has to say so."""
    from relay.telegram import words
    out = words.sync_report([(datetime.date(2026, 10, 5), 4272)], 1)
    assert "Nothing was submitted" in out


def test_the_sync_report_uses_the_singular_for_one_day(bot):
    from relay.telegram import words
    out = words.sync_report([(datetime.date(2026, 10, 5), 4272)], 1)
    assert "Synced 1 day" in out
    assert "days" not in out.split("\n")[0]


def test_an_empty_sync_says_so_rather_than_claiming_success(bot):
    from relay.telegram import words
    out = words.sync_empty()
    assert "no days" in out.lower()
    assert "Nothing was stored" in out


def test_the_sync_prompt_says_it_submits_nothing(bot):
    from relay.telegram import words
    assert "submits nothing" in words.syncing()
