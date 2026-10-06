"""Reading the dashboard back, without a browser.

Everything in here is pure: strings in, dates and numbers out. It lives apart
from driver.py on purpose, because a regex that stops matching the Site's
markup fails silently -- read_days() just returns fewer rows -- and the thing
most likely to break must be the thing easiest to test.

The DOM this is written against, from the live dashboard:

    <ul class="space-y-2">
      <li class="surface-card flex items-center justify-between p-3 text-sm">
        <div class="min-w-0 flex-1">
          <p class="font-semibold text-foreground">5 Oct 2026</p>
          <p class="text-xs text-muted-foreground">Daily steps</p>
        </div>
        <span class="ml-3 ...">+4,272 steps</span>
      </li>
    </ul>
"""

import datetime
import re
from typing import TypedDict

# The dashboard writes dates as "5 Oct 2026" -- day, abbreviated month, year,
# with no leading zero on the day.
DAY_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$")


# Month name -> number, abbreviated and full both. Named MONTH_NUM because
# driver.py already has MONTHS: a tuple of full names, used by the date-picker
# parsing. Same name in one module would shadow it and break set_date().
#
# Accepting the full names as well as the abbreviations is not just tolerance:
# the dashboard has been seen writing "5 Oct 2026", and a site that switches to
# "5 October 2026" should still sync rather than silently record nothing.
class Profile(TypedDict):
    """The user's own totals from /home: None for whatever is missing."""

    total_steps: int | None
    total_points: int | None
    house: str | None


MONTH_NUM = {
    m.lower(): i
    for i, m in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        start=1,
    )
}
MONTH_NUM.update(
    {
        m.lower(): i
        for i, m in enumerate(
            (
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
            ),
            start=1,
        )
    }
)
# The step badge reads "+4,272 steps".
STEPS_RE = re.compile(r"^\+?([\d,]+)\s*steps?$", re.I)


def parse_day(text: str | None) -> datetime.date | None:
    """'5 Oct 2026' -> datetime.date(2026, 10, 5), or None.

    Returns None rather than raising, because this is parsing a page: a row we
    do not understand is skipped, not a reason to fail the whole sync.
    """
    m = DAY_RE.match((text or "").strip())
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2).lower(), m.group(3)
    month = MONTH_NUM.get(mon)
    if month is None:
        return None
    try:
        return datetime.date(int(year), month, int(day))
    except ValueError:
        return None


def parse_steps(text: str | None) -> int | None:
    """'+4,272 steps' -> 4272, or None."""
    m = STEPS_RE.match((text or "").strip())
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_profile(board_text: str) -> Profile | None:
    """Pull the user's own totals out of the /home DOM text, if present.

    Returns {"total_steps", "total_points", "house"} with None for whatever is
    missing, or None when the board holds nothing at all. Formatting is the
    caller's job: this module reads the Site, it does not write Telegram copy.
    """
    total_steps: int | None = None
    total_points: int | None = None
    m = re.search(r"([\d,]+)\s*\n?\s*total steps", board_text, re.I)
    if m:
        total_steps = int(m.group(1).replace(",", ""))
    m = re.search(r"([\d,]+)\s*\n?\s*total points", board_text, re.I)
    if m:
        total_points = int(m.group(1).replace(",", ""))
    house: str | None = None
    m = re.search(r"YOUR HOUSE\s*\n+\s*([A-Za-z]+)", board_text)
    if m:
        house = m.group(1)
    if total_steps is None and total_points is None and house is None:
        return None
    return {"total_steps": total_steps, "total_points": total_points, "house": house}
