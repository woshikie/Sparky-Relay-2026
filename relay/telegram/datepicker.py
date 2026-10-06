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

# `datepicker` and `cb_date` must agree on this. Asserted in the tests, because
# the failure is a silent no-op rather than an error.
PREFIX = "dt"


def shift(year, month, delta):
    """(year, month) moved by `delta` months, wrapped.

    Python's date() refuses month 0 and month 13, so the wrap has to be done
    here rather than left to the arithmetic in a callback handler.
    """
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def month_title(year, month):
    """'October 2026' — the caption the grid otherwise lacks."""
    return "%s %d" % (MONTH_NAMES[month - 1], year)


def month_grid(year, month, today=None):
    """Rows of 7 cells for the given month, Monday-first.

    Cells are `datetime.date` or None for the padding either side.
    """
    today = today or datetime.date.today()
    first = datetime.date(year, month, 1)
    lead = first.weekday()
    days = calendar.monthrange(year, month)[1]
    cells = [None] * lead
    for d in range(1, days + 1):
        cells.append(datetime.date(year, month, d))
    while len(cells) % 7:
        cells.append(None)
    return [cells[i : i + 7] for i in range(0, len(cells), 7)], today


def selectable(day, today):
    """Only past and present days: the site has no future entries to replace."""
    return day <= today


def keyboard(year, month, today=None):
    """InlineKeyboardMarkup for one month.

    `year`/`month` are clamped forward to the current month, because paging
    past today offers only dead buttons and reads as a broken calendar.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    today = today or datetime.date.today()
    if (year, month) < (today.year, today.month):
        year, month = today.year, today.month

    py, pm = shift(year, month, -1)
    ny, nm = shift(year, month, 1)

    kb = [
        [
            InlineKeyboardButton("«", callback_data="%s:prev:%d:%d" % (PREFIX, py, pm)),
            # The month itself. Without it there is no way to tell which month the
            # grid is showing, which is the one thing a calendar has to say.
            InlineKeyboardButton(
                month_title(year, month), callback_data="%s:none" % PREFIX
            ),
            InlineKeyboardButton("»", callback_data="%s:next:%d:%d" % (PREFIX, ny, nm)),
        ]
    ]
    kb.append(
        [InlineKeyboardButton(w, callback_data="%s:none" % PREFIX) for w in WEEKDAYS]
    )
    for row in month_grid(year, month, today)[0]:
        line = []
        for c in row:
            if c is None:
                line.append(InlineKeyboardButton(" ", callback_data="%s:none" % PREFIX))
            elif selectable(c, today):
                line.append(
                    InlineKeyboardButton(
                        str(c.day), callback_data="%s:day:%s" % (PREFIX, c.isoformat())
                    )
                )
            else:
                # Future days stay visible but inert, so the shape of the
                # month is still legible. A hidden cell would leave a gap.
                line.append(
                    InlineKeyboardButton(str(c.day), callback_data="%s:none" % PREFIX)
                )
        kb.append(line)
    kb.append(
        [
            InlineKeyboardButton("Today", callback_data="%s:today" % PREFIX),
        ]
    )
    return InlineKeyboardMarkup(kb)


def callback_captions(markup):
    """Every callback_data in the markup, for tests and for routing checks."""
    return [b.callback_data for row in markup.inline_keyboard for b in row]
