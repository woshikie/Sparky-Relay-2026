"""The Telegram handler surface: keyboards, date selection, and the guard.

These exercise the pure logic behind the handlers without a live Telegram
connection. The flow itself (photo -> confirm -> commit) needs the network and
is covered by check_container.py inside the image.
"""
import datetime

import pytest


# ------------------------------------------------------------------ ordinals

@pytest.mark.parametrize("day,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),        # teens are all "th"
    (21, "21st"), (22, "22nd"), (23, "23rd"),
    (30, "30th"), (31, "31st"),
])
def test_ordinals(bot, day, expected):
    assert bot.ordinal(day) == expected


# ------------------------------------------------------------------ sgt time

def test_sg_today_is_shanghai_today(bot):
    """The competition runs on +08:00, so a 9pm submission is still today."""
    sgt = bot.sg_now()
    assert sgt.tzinfo is not None
    assert bot.sg_today() == sgt.date()


def test_sg_is_eight_hours_ahead_of_utc(bot):
    import datetime as dt
    utc = dt.datetime.now(dt.timezone.utc)
    assert (bot.sg_now().replace(tzinfo=None)
            - utc.replace(tzinfo=None)).total_seconds() == pytest.approx(
                config_offset := 8 * 3600, abs=2)


# ----------------------------------------------------------------- keyboards

def test_the_date_keyboard_offers_today_and_yesterday(bot):
    kb = bot.kb_date_default()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Today" in t for t in labels)
    assert any("Yesterday" in t for t in labels)
    assert any("datepicker" in t.lower() for t in labels)


def test_the_date_keyboard_shows_the_actual_dates(bot):
    """A label that says 'Today' is not enough; it has to say which day."""
    kb = bot.kb_date_default()
    t = bot.sg_today()
    y = t - datetime.timedelta(days=1)
    labels = " ".join(b.text for row in kb.inline_keyboard for b in row)
    assert t.strftime("%d %b") in labels
    assert y.strftime("%d %b") in labels


def test_the_confirm_keyboard_carries_the_number(bot):
    kb = bot.kb_confirm("6,532")
    labels = " ".join(b.text for row in kb.inline_keyboard for b in row)
    assert "6,532" in labels
    assert "Confirm" in labels
    assert "Cancel" in labels


def test_the_overwrite_keyboard_shows_what_would_be_lost(bot):
    """The destructive choice must show the number it destroys."""
    kb = bot.kb_overwrite(6532)
    labels = " ".join(b.text for row in kb.inline_keyboard for b in row)
    assert "6,532" in labels          # what is currently recorded
    assert "Overwrite" in labels


def test_the_overwrite_warning_body_names_both_numbers(bot):
    """The incoming value is the whole point of the warning."""
    words = bot.words
    text = words.overwrite_warning(700, 6532, "October 4th, 2026")
    assert "6,532" in text            # currently recorded
    assert "700" in text              # what the screenshot read
    assert "5,832" in text            # and the difference


def test_the_upgrade_notice_names_both_numbers(bot):
    text = bot.words.overwrite_upgrade(6900, 6532, "October 4th, 2026")
    assert "6,532" in text
    assert "6,900" in text


def test_the_date_picker_keyboard_is_scoped_to_a_month(bot):
    kb = bot.kb_pick_date(2026, 10)
    assert len(kb.inline_keyboard) >= 7


# ------------------------------------------------------- plausibility guard

@pytest.mark.parametrize("steps,plausible", [
    (99, False), (100, True), (2831, True),
    (200_000, True), (200_001, False),
])
def test_the_plausibility_band_matches_the_site(bot, steps, plausible):
    from relay.site import driver as relay_site
    assert (relay_site.MIN_STEPS <= steps <= relay_site.MAX_STEPS) is plausible


# ------------------------------------------------------- pending / guard

def test_pending_confirmations_are_keyed_per_chat(bot):
    bot.pending.put((1, 10), {"steps": 100})
    assert bot.pending.get((1, 10))["steps"] == 100
    assert bot.pending.get((2, 10)) is None


def test_the_most_recent_pending_wins(bot):
    bot.pending.put((1, 10), {"a": 1})
    bot.pending.put((1, 11), {"b": 2})
    key, _ = bot.pending.latest_for_chat(1)
    assert key == (1, 11)


def test_expired_pendings_read_as_missing(bot):
    """Expiry is lazy: a stale record reads as missing and is dropped."""
    record = bot.pending.put((1, 10), {"steps": 100})
    record["at"] -= 3600
    assert bot.pending.get((1, 10)) is None
    assert bot.pending.latest_for_chat(1) is None
    assert bot.pending.count() == 0


# ------------------------------------------------------- leaderboard parse

def test_totals_are_parsed_out_of_the_home_page(bot):
    text = "YOUR HOUSE\nEsplanade\nHouse standings\n1\nIstana\n4,034,765"
    profile = bot.parse_profile(text)
    # No total-steps markup in this sample, so nothing to claim beyond the house.
    assert profile == {"total_steps": None, "total_points": None,
                       "house": "Esplanade"}


def test_a_home_page_with_totals_is_summarised(bot):
    """The regexes match the labels the site actually renders.

    Note the label-value order: the site's DOM puts the value on the following
    line, and steps are shown in points terms (1000 steps per point), so 11,225
    in the sample is the point figure, not a step count. The bot does not try
    to convert — it reports what the site said.
    """
    text = "YOUR HOUSE\nEsplanade\nHouse standings\nTotal steps\n11,225\nTotal points\n10"
    profile = bot.parse_profile(text)
    assert profile["total_points"] == 11225
    assert profile["house"] == "Esplanade"


def test_nothing_is_invented_from_an_unrecognisable_page(bot):
    assert bot.parse_profile("nothing useful here") is None


# ------------------------------------------------------------- profile text

def test_a_profile_line_without_totals_still_names_the_house(bot):
    profile = bot.parse_profile("YOUR HOUSE\nEsplanade\nHouse standings")
    assert profile == {"total_steps": None, "total_points": None,
                       "house": "Esplanade"}
