"""Telegram-side date picker: a month grid of inline buttons.

The site has its own Radix calendar, but the user should not have to learn its
navigation. We ask in Telegram, then hand the Relay a plain date object.
"""
import calendar
import datetime


def month_grid(year, month, today=None):
    """Return rows of 7 label/offset cells for the given month.

    `today` marks the button to mark as default.
    """
    today = today or datetime.date.today()
    first = datetime.date(year, month, 1)
    # Monday-first, matching the site's own calendar
    lead = first.weekday()
    days = calendar.monthrange(year, month)[1]
    cells = [None] * lead
    for d in range(1, days + 1):
        cells.append(datetime.date(year, month, d))
    while len(cells) % 7:
        cells.append(None)
    rows = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    return rows, today


def keyboard(year, month, today=None):
    """InlineKeyboardMarkup for one month."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows, today = month_grid(year, month, today)
    kb = [[InlineKeyboardButton("«", callback_data="cal:prev:%d:%d" % (year, month))]]
    week = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    kb.append([InlineKeyboardButton(w, callback_data="cal:none") for w in week])
    for row in rows:
        line = []
        for c in row:
            if c is None:
                line.append(InlineKeyboardButton(" ", callback_data="cal:none"))
            elif c == today:
                line.append(InlineKeyboardButton(str(c.day), callback_data="cal:day:%s" % c.isoformat()))
            elif c > today:
                line.append(InlineKeyboardButton(str(c.day), callback_data="cal:none"))
            else:
                line.append(InlineKeyboardButton(str(c.day), callback_data="cal:day:%s" % c.isoformat()))
        kb.append(line)
    kb.append([
        InlineKeyboardButton("Today", callback_data="cal:today"),
        InlineKeyboardButton("»", callback_data="cal:next:%d:%d" % (year, month)),
    ])
    return InlineKeyboardMarkup(kb)
