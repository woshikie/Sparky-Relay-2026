"""The month grid, and the promise that tapping a day actually does something.

The bug this file exists for: `datepicker.keyboard` emitted `cal:` callbacks
while `cb_date` was registered on `^dt:`. Every tap was dropped by the router,
so Telegram left the button spinning and the calendar looked frozen — which the
user reported as "stuck", and which no test noticed because the grid had no
tests at all.

So the first test here is the routing one, and it reads the registration table
out of bot.py rather than trusting the two modules to stay in step.
"""
import datetime
import importlib

import pytest

from relay.telegram import datepicker


@pytest.fixture
def bot():
    return importlib.import_module("relay.telegram")


def captions(markup):
    return datepicker.callback_captions(markup)


def day_rows(markup):
    """Only the rows of day cells.

    The header (arrows + month title) and the footer (Today) are not 7 wide,
    which is correct -- so a blanket seven-wide assertion would be testing the
    wrong thing and would fail on a right answer.
    """
    return markup.inline_keyboard[2:-1]


def day_cells(markup):
    return [b for row in day_rows(markup) for b in row]


# ------------------------------------------------------------- routing

def test_every_callback_reaches_a_registered_handler(bot):
    """The regression.

    Reads the real handler patterns instead of asserting the prefix twice, so
    renaming either side breaks this rather than silently hanging a button.
    """
    import re
    patterns = _patterns(bot)
    assert patterns, "no callback handlers found in main()"
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    for data in captions(grid):
        assert any(re.match(p, data) for p in patterns), (data, patterns)


def test_the_grid_uses_the_prefix_the_router_matches(bot):
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    prefixes = {d.split(":")[0] for d in captions(grid)}
    assert prefixes == {"dt"}, prefixes


def _patterns(bot):
    """The callback patterns bot.main() actually registers.

    Read out of the source because the handler table is built inside main(),
    so there is no attribute to assert against -- which is exactly why the
    prefix and the registration drifted apart without anything noticing.
    """
    import inspect
    import re
    src = inspect.getsource(bot.main)
    return re.findall(r'CallbackQueryHandler\([^,]+,\s*pattern=r"([^"]+)"', src)


def test_a_day_tap_carries_a_real_date(bot):
    """One october 2026, which is the date the user tried to pick."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-03" in captions(grid)


def test_the_day_the_user_wanted_is_reachable(bot):
    """The 3rd is in the past relative to the 6th, so it must be tappable."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-03" in captions(grid)


# --------------------------------------------------------------- content

def test_the_grid_names_the_month():
    """Without this there is no way to tell which month you are looking at."""
    kb = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6)).inline_keyboard
    labels = [b.text for b in kb[0]]
    assert any("October" in l for l in labels), labels
    assert any("2026" in l for l in labels), labels


@pytest.mark.parametrize(
    "year, month, today",
    [
        (2026, 9, datetime.date(2026, 10, 6)),
        (2026, 8, datetime.date(2026, 10, 6)),
        (2025, 12, datetime.date(2026, 10, 6)),
        # Discriminating: with today in November, a floor-less clamp to
        # today would show November, not October.
        (2026, 9, datetime.date(2026, 11, 5)),
    ],
)
def test_months_before_the_event_show_october(year, month, today):
    """The event runs October 2026 only: September is not a month here."""
    grid = datepicker.keyboard(year, month, today)
    assert grid.inline_keyboard[0][1].text == "October 2026"


def test_a_far_future_month_snaps_to_the_current_month():
    """Future months are unreachable: the grid lands on today's month."""
    grid = datepicker.keyboard(2031, 5, datetime.date(2026, 10, 6))
    assert grid.inline_keyboard[0][1].text == "October 2026"


def test_the_title_is_a_button_not_a_link():
    """It has to be inert, or tapping it would try to navigate."""
    kb = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6)).inline_keyboard
    title = [b for b in kb[0] if "October" in b.text]
    assert title and title[0].callback_data == "dt:none"


def test_the_weekday_row_is_monday_first():
    kb = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6)).inline_keyboard
    assert [b.text for b in kb[1]] == list(datepicker.WEEKDAYS)
    assert datepicker.WEEKDAYS[0] == "Mo"


def test_today_is_selectable():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-06" in captions(grid)


def test_future_days_carry_a_day_payload():
    """Judged server-side at tap time, not at render time.

    A grid rendered before SGT midnight must not misjudge taps after it,
    so every real day carries dt:day and _cb_date rejects future ones.
    """
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-07" in captions(grid)


def test_future_days_are_still_visible():
    """A gap would make the month look broken rather than closed."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "31" in [b.text for b in day_cells(grid)]


def test_no_future_payload_shape():
    """The dt:future: shape is gone: one day shape, judged at tap time."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert not any(d.startswith("dt:future:") for d in captions(grid))


def test_padding_is_still_silent():
    """Padding, weekdays and title stay none; only real days tap through.

    The Today shortcut in the same markup is live dt:today by design —
    this is about the grid cells, not that footer button."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    blanks = [b for b in day_cells(grid) if not b.text.strip()]
    assert blanks, "expected padding cells in this month"
    assert all(b.callback_data == "dt:none" for b in blanks)


def test_there_is_a_today_shortcut():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:today" in captions(grid)


# ---------------------------------------------------------------- shape

def test_every_day_row_is_seven_wide():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    for row in day_rows(grid):
        assert len(row) == 7, [b.text for b in row]


@pytest.mark.parametrize(
    "today, year, month, title",
    [
        (datetime.date(2026, 10, 6), 2026, 10, "October 2026"),
        (datetime.date(2026, 11, 5), 2026, 10, "October 2026"),
        (datetime.date(2026, 11, 5), 2026, 11, "November 2026"),
        (datetime.date(2026, 12, 10), 2026, 12, "December 2026"),
    ],
)
def test_every_month_is_a_whole_number_of_weeks(today, year, month, title):
    """Padding exists precisely so the last week is not ragged."""
    grid = datepicker.keyboard(year, month, today)
    assert grid.inline_keyboard[0][1].text == title
    assert all(len(r) == 7 for r in day_rows(grid)), (title, grid)


def test_the_first_of_the_month_lands_under_the_right_weekday():
    """1 Oct 2026 is a Thursday, so it sits under 'Th'."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    for row in day_rows(grid):
        labels = [b.text.strip() for b in row]
        if "1" in labels:
            assert datepicker.WEEKDAYS[labels.index("1")] == "Th", labels
            return
    pytest.fail("the 1st is not in the grid")


# ------------------------------------------------------------ navigation

def test_shift_back_wraps_into_the_previous_year():
    assert datepicker.shift(2026, 1, -1) == (2025, 12)


def test_shift_forward_wraps_into_the_next_year():
    assert datepicker.shift(2026, 12, 1) == (2027, 1)


def test_shift_back_within_the_year():
    assert datepicker.shift(2026, 3, -1) == (2026, 2)


def test_shift_over_a_year():
    assert datepicker.shift(2026, 11, 3) == (2027, 2)


def test_the_back_arrow_is_disabled_at_the_start_of_the_event():
    """October 2026 is the floor: paging back goes nowhere."""
    kb = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6)).inline_keyboard
    assert kb[0][0].text == "«"
    assert kb[0][0].callback_data == "dt:none"


def test_the_next_arrow_is_disabled_at_the_current_month():
    """Paging past today goes nowhere."""
    kb = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6)).inline_keyboard
    assert kb[0][2].text == "»"
    assert kb[0][2].callback_data == "dt:none"


def test_paging_inside_the_window_names_resolved_months():
    """Arrows with somewhere to go still carry the target month resolved."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 11, 5))
    assert "dt:next:2026:11" in captions(grid)
    grid = datepicker.keyboard(2026, 11, datetime.date(2026, 11, 5))
    assert "dt:prev:2026:10" in captions(grid)


def test_navigation_carries_a_resolved_month_not_an_offset():
    """The wrap belongs to datepicker, not to a second copy in the handler.

    The handler used to subtract one and re-wrap, which meant two files had to
    agree about January.
    """
    jan = datepicker.keyboard(2027, 1, datetime.date(2027, 1, 15))
    assert "dt:prev:2026:12" in captions(jan)


# --------------------------------------------------------------- clamping

def test_a_month_in_the_past_snaps_to_the_present():
    """Paging backwards past today offers only dead buttons."""
    grid = datepicker.keyboard(2026, 8, datetime.date(2026, 10, 6))
    kb = grid.inline_keyboard
    assert any("October" in b.text for b in kb[0]), [b.text for b in kb[0]]


def test_clamping_does_not_lose_the_days():
    grid = datepicker.keyboard(2026, 8, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-03" in captions(grid)


def test_a_future_month_snaps_to_the_present():
    """Paging forward past today lands back on today's month, every day
    carrying a day payload."""
    grid = datepicker.keyboard(2026, 12, datetime.date(2026, 10, 6))
    kb = grid.inline_keyboard
    assert any("October" in b.text for b in kb[0]), [b.text for b in kb[0]]


def test_today_defaults_to_the_app_clock(monkeypatch):
    """Omitting `today` reads the SGT clock, never the system-local date."""
    import relay.clock as clock_mod
    import relay.telegram.datepicker as fresh

    seen = []

    def fake():
        seen.append(True)
        return datetime.date(2026, 12, 6)

    monkeypatch.setattr(clock_mod, "sg_today", fake)
    grid = fresh.keyboard(2031, 5)
    assert seen, "keyboard() must read the app SGT clock"
    assert grid.inline_keyboard[0][1].text == "December 2026"


def test_a_pre_event_today_pins_to_a_dead_october():
    """Before the event the window is degenerate: October, visibly so."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 1, 15))
    assert grid.inline_keyboard[0][1].text == "October 2026"
    assert "dt:day:2026-10-01" in captions(grid)


# --------------------------------------------------------- the sealed bit

@pytest.mark.parametrize(
    "today, year, month",
    [
        (datetime.date(2026, 11, 5), 2026, 10),
        (datetime.date(2026, 11, 5), 2026, 11),
        # December-to-January wrap: the prev arrow must name December.
        (datetime.date(2027, 1, 15), 2027, 1),
        # Forward year-wrap: December stays in-window under a January
        # ceiling, so the next arrow live-names January.
        (datetime.date(2027, 1, 15), 2026, 12),
    ],
)
def test_the_grid_has_no_navigation_to_a_non_month(today, year, month):
    """shift()'s wrap must never emit month 0 or 13 onto the wire.

    In-window todays, so the arrows carry real prev/next payloads that
    are actually inspected — a fully clamped grid has only silent buttons
    and the loop below would assert nothing.
    """
    grid = datepicker.keyboard(year, month, today)
    nav = [d for d in captions(grid) if d.startswith(("dt:prev:", "dt:next:"))]
    assert nav, "expected live arrows for an in-window month"
    for data in nav:
        _, _, _, mm = data.split(":")
        assert 1 <= int(mm) <= 12, data
