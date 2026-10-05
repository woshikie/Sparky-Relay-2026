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

import datepicker


@pytest.fixture
def bot():
    return importlib.import_module("bot")


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


def test_every_month_is_named():
    # today = 15 Jan, so no month is in the past and none gets clamped away.
    for m in range(1, 13):
        kb = datepicker.keyboard(2026, m, datetime.date(2026, 1, 15)).inline_keyboard
        assert any(datepicker.MONTH_NAMES[m - 1] in b.text for b in kb[0]), m


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


def test_future_days_are_inert():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:day:2026-10-07" not in captions(grid)


def test_future_days_are_still_visible():
    """A gap would make the month look broken rather than closed."""
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "31" in [b.text for b in day_cells(grid)]


def test_there_is_a_today_shortcut():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:today" in captions(grid)


# ---------------------------------------------------------------- shape

def test_every_day_row_is_seven_wide():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    for row in day_rows(grid):
        assert len(row) == 7, [b.text for b in row]


def test_every_month_is_a_whole_number_of_weeks():
    """Padding exists precisely so the last week is not ragged."""
    for m in range(1, 13):
        grid = datepicker.keyboard(2026, m, datetime.date(2026, 1, 15))
        assert all(len(r) == 7 for r in day_rows(grid)), m


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


def test_the_back_button_names_the_previous_month():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:prev:2026:9" in captions(grid)


def test_the_next_button_names_the_next_month():
    grid = datepicker.keyboard(2026, 10, datetime.date(2026, 10, 6))
    assert "dt:next:2026:11" in captions(grid)


def test_navigation_carries_a_resolved_month_not_an_offset():
    """The wrap belongs to datepicker, not to a second copy in the handler.

    The handler used to subtract one and re-wrap, which meant two files had to
    agree about January.
    """
    jan = datepicker.keyboard(2027, 1, datetime.date(2026, 12, 31))
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


def test_a_future_month_is_left_alone():
    """Only the past is clamped; the future grid is just empty, not rewritten."""
    grid = datepicker.keyboard(2026, 12, datetime.date(2026, 10, 6))
    kb = grid.inline_keyboard
    assert any("December" in b.text for b in kb[0]), [b.text for b in kb[0]]


def test_today_defaults_to_the_real_today():
    """A caller that forgets to pass `today` gets sane behaviour, not a crash."""
    kb = datepicker.keyboard(datetime.date.today().year,
                             datetime.date.today().month).inline_keyboard
    assert any(str(datetime.date.today().year) in b.text for b in kb[0])


# --------------------------------------------------------- the sealed bit

def test_the_grid_has_no_navigation_to_a_non_month():
    """date() would raise on month 0 or 13, so the wrap must not produce one."""
    for m in range(1, 13):
        grid = datepicker.keyboard(2026, m, datetime.date(2026, 1, 1))
        # January of the next year is the only month not clamped forward.
        for data in captions(grid):
            if data.startswith(("dt:prev:", "dt:next:")):
                y, mm = data.split(":")[2:]
                assert 1 <= int(mm) <= 12, data
                assert int(mm) != 0, data
