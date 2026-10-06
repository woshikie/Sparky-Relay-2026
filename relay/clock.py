"""Singapore time.

The Site runs on SGT, so every date the bot reasons about is an SGT date.
Used by the keyboards, the callbacks, and the nightly backup.
"""
import datetime

from relay import config


def sg_now():
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        hours=config.SGT_OFFSET_HOURS)


def sg_today():
    return sg_now().date()
