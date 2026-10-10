"""Telegram-side date picker: a month grid of inline buttons.

The site has its own Radix calendar, but the user should not have to learn its
navigation. We ask in Telegram, then hand the Relay a plain date object.

Telegram's Bot API has no date-picker widget — buttons are the only input a
plain bot can offer. The alternative is a Mini App, which needs a publicly
trusted HTTPS origin; a self-signed certificate on a free host will not do, so
that would mean buying a domain. Hence the grid.

Two rules this module owns, because getting either wrong is invisible until
someone taps:

  * callback_data starts with `dt:`, the prefix `cb_date` is registered on. It
    used to be `cal:` here while the handler matched `^dt:`, so every tap was
    dropped and the button spun forever. Nothing asserted the two agreed.
  * prev/next carry the *target* month already resolved. Shifting was left to
    the handler, so the wrap from January to December lived in a second file.
"""

import calendar
import datetime
from typing import cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import relay.clock as clock
import relay.telegram.codec as codec

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

# The event runs October 2026 only, so the grid never leaves it: earlier
# months snap forward to October, later months snap back to today's.
EVENT_START_YEAR = 2026
EVENT_START_MONTH = 10

# The prefix lives in the codec now, next to every other wire shape.
# Aliased, not repeated, so datepicker.PREFIX keeps resolving.
PREFIX = codec.PREFIX


def shift(year: int, month: int, delta: int) -> tuple[int, int]:
    """(year, month) moved by `delta` months, wrapped.

    Python's date() refuses month 0 and month 13, so the wrap has to be done
    here rather than left to the arithmetic in a callback handler.
    """
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def month_title(year: int, month: int) -> str:
    """'October 2026' — the caption the grid otherwise lacks."""
    return "%s %d" % (MONTH_NAMES[month - 1], year)


def month_grid(
    year: int, month: int, today: datetime.date | None = None
) -> tuple[list[list[datetime.date | None]], datetime.date]:
    """Rows of 7 cells for the given month, Monday-first.

    Cells are `datetime.date` or None for the padding either side.
    """
    today = today or clock.sg_today()
    first = datetime.date(year, month, 1)
    lead = first.weekday()
    days = calendar.monthrange(year, month)[1]
    cells: list[datetime.date | None] = [None] * lead
    for d in range(1, days + 1):
        cells.append(datetime.date(year, month, d))
    while len(cells) % 7:
        cells.append(None)
    return [cells[i : i + 7] for i in range(0, len(cells), 7)], today


def _ceiling(today: datetime.date) -> tuple[int, int]:
    """(year, month) of the newest month the grid may show.

    Pinned to at least the event start: before October 2026 the window
    is degenerate, and the grid shows a dead October rather than an
    empty or backwards range.
    """
    eff = max(today, datetime.date(EVENT_START_YEAR, EVENT_START_MONTH, 1))
    return eff.year, eff.month


def clamp(year: int, month: int, today: datetime.date) -> tuple[int, int]:
    """Snap (year, month) into the window; outside snaps to an edge."""
    if (year, month) < (EVENT_START_YEAR, EVENT_START_MONTH):
        return EVENT_START_YEAR, EVENT_START_MONTH
    if (year, month) > _ceiling(today):
        return _ceiling(today)
    return year, month


def keyboard(
    year: int, month: int, today: datetime.date | None = None
) -> InlineKeyboardMarkup:
    """InlineKeyboardMarkup for one month.

    `year`/`month` are clamped into [October 2026, the current month]:
    paging earlier than the event offers only dead buttons, and paging past
    today offers only future ones. A nav arrow whose target falls outside
    the window is rendered as a silent button, so paging cannot leave it.

    Every real day carries a day payload, future or not: whether the day
    has happened yet is judged server-side at tap time, because a grid
    rendered before SGT midnight would otherwise misjudge taps after it.
    """
    today = today or clock.sg_today()
    year, month = clamp(year, month, today)

    py, pm = shift(year, month, -1)
    ny, nm = shift(year, month, 1)
    # shift() owns the year wrap; the clamp owns the window. A target the
    # clamp would move keeps its arrow shape but goes nowhere.
    prev_data = (
        codec.date("none")
        if clamp(py, pm, today) != (py, pm)
        else codec.date("prev", py, pm)
    )
    next_data = (
        codec.date("none")
        if clamp(ny, nm, today) != (ny, nm)
        else codec.date("next", ny, nm)
    )

    kb = [
        [
            InlineKeyboardButton("«", callback_data=prev_data),
            # The month itself. Without it there is no way to tell which month the
            # grid is showing, which is the one thing a calendar has to say.
            InlineKeyboardButton(
                month_title(year, month), callback_data=codec.date("none")
            ),
            InlineKeyboardButton("»", callback_data=next_data),
        ]
    ]
    kb.append(
        [InlineKeyboardButton(w, callback_data=codec.date("none")) for w in WEEKDAYS]
    )
    for row in month_grid(year, month, today)[0]:
        line = []
        for c in row:
            if c is None:
                line.append(InlineKeyboardButton(" ", callback_data=codec.date("none")))
            else:
                line.append(
                    InlineKeyboardButton(
                        str(c.day), callback_data=codec.date("day", c.isoformat())
                    )
                )
        kb.append(line)
    kb.append(
        [
            InlineKeyboardButton("Today", callback_data=codec.date("today")),
        ]
    )
    return InlineKeyboardMarkup(kb)


def callback_captions(markup: InlineKeyboardMarkup) -> list[str]:
    """Every callback_data in the markup, for tests and for routing checks."""
    # PTB types callback_data as str | object (arbitrary-callback-data
    # feature); this module only ever builds str buttons, hence the cast.
    return [cast(str, b.callback_data) for row in markup.inline_keyboard for b in row]
