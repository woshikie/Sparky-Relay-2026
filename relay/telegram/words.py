"""Telegram chat copy. Kept apart so wording changes are one file.

Two rules for anything in here:

1. Dynamic values are never interpolated raw. They go through `md()` (escape)
   or `code()` (literal). Every bug in this file so far has been a value that
   skipped that: `{'name': ...}` printed literally, `whitelist_claim`
   silently killing a whole message.
2. Literal markup in the templates is fine and deliberate. Only interpolated
   values need escaping.
"""

import datetime

from telegram.helpers import escape_markdown


def md(text: object) -> str:
    """Escape a value for interpolation into a Telegram Markdown message.

    This is python-telegram-bot's own `escape_markdown`, not a hand-rolled
    version: it is maintained alongside the library that sends the message, and
    it knows which version of the dialect it targets.

    It matters more than it looks. `_` is an italic delimiter in Telegram's
    legacy Markdown, and identifiers are full of them — `whitelist_claim`
    contains one — so an unescaped mode name opens an italic that never closes.
    Telegram rejects the *whole* message, with no error locally and nothing
    visible in the client. That is how `/status` came to return nothing at all
    while every test passed.

    Note that a CommonMark parser will NOT catch this: CommonMark leaves
    intra-word underscores alone, Telegram does not. Validating with the wrong
    dialect is worse than not validating, because it looks like it worked.
    """
    return escape_markdown(str(text))


def code(text: object) -> str:
    """Render a value as a code span, where Markdown is not interpreted.

    Preferred over escaping for identifiers and paths: it renders them as
    literal values rather than as prose, and it cannot be broken by whatever
    character the value happens to contain.
    """
    return "`%s`" % str(text).replace("`", "")


EMOJI = {
    "start": "\U0001f3af",
    "scan": "\U0001f50d",
    "ocr": "\U0001f9e0",
    "date": "\U0001f4c5",
    "ok": "✅",
    "no": "❌",
    "warn": "⚠️",
    "overwrite": "\U0001f504",
    "rank": "\U0001f3c5",
    "backup": "\U0001f4be",
    "log": "\U0001f4dc",
    "sync": "\U0001f504",
}

HELP = (
    "%(start)s Send me a screenshot of your step tracker's day view "
    "and I will read the step count the site reports, then ask you to confirm "
    "the date before anything is recorded.\n\n"
    "Commands:\n"
    "/login — supply your site username and password\n"
    "/logout — forget the stored credentials\n"
    "/log — recent Submissions\n"
    "/status — access mode, credentials, and memory state\n"
    "/help — this message"
)

# --- access -------------------------------------------------------------


def claimed() -> str:
    return (
        "🔐 **This chat now owns the Relay.**\n\n"
        "Only chats listed in `/status` can drive it. Remove a chat from the "
        "list to revoke its access.\n\n" + HELP
    )


def already_claimed() -> str:
    return (
        "🔐 The Relay has already been claimed by another chat.\n\n"
        "Access Mode is `whitelist_claim`, so only the first chat to `/start` "
        "gets in. If that should be you, revoke the existing claim first — "
        "or switch `ACCESS_MODE` in `secrets.env`."
    )


def secret_prompt() -> str:
    return (
        "🔑 This Relay needs a Shared Secret.\n\n"
        "Send it here and I will remember this chat. I delete the message as "
        "soon as I read it, but a notification may already have shown — so "
        "send it in a private chat, not a group.\n\n"
        "Wrong guesses are rate limited, not locked out: nobody can lock you "
        "out of your own bot this way."
    )


def secret_accepted(how: str | None) -> str:
    label = (how or "").split(":", 1)[-1] or "a configured secret"
    return "🔑 Secret accepted (`%s`). This chat now has access." % label


def secret_rejected() -> str:
    return (
        "❌ That is not a valid Shared Secret.\n\n"
        "I have deleted the message. Try again, or wait a moment if you are "
        "being rate limited."
    )


def secret_throttled(wait: float) -> str:
    return (
        "⏳ Too many attempts. Try again in %d second%s.\n\n"
        "This is a rate limit, not a lockout — you cannot be locked out."
        % (wait, "" if wait == 1 else "s")
    )


def access_refused(decision: object) -> str:
    why = getattr(decision, "why", "denied")
    if why == "denied":
        return (
            "⛔ This chat is on the deny list.\n\n"
            "Chat id: `%s`\n\n"
            "Remove it from `DENY_CHAT_IDS` in secrets.env, or have the owner "
            "run `/undeny %s`." % (_chat_id_of(decision), _chat_id_of(decision))
        )
    if why == "secret_throttled":
        return secret_throttled(getattr(decision, "retry_after", 30))
    if why == "needs_secret":
        return secret_prompt()
    if why == "needs_claim":
        return already_claimed()
    return (
        "⛔ This chat cannot drive the Relay.\n\n"
        "Access Mode is `%s`. Chat id: `%s`"
        % (_mode_or_unknown(), _chat_id_of(decision))
    )


def _chat_id_of(decision: object) -> object:
    return getattr(decision, "chat_id", "unknown")


def _mode_or_unknown() -> str:
    from relay import config

    return config.ACCESS_MODE


# --- credentials prompt -------------------------------------------------


def choose_preset(username: str) -> str:
    return (
        "🔑 I have preset credentials for **%s**.\n\n"
        "Use those, or give me different ones? The password is never shown — "
        "I am only telling you which account this would be." % username
    )


def using_preset(username: str) -> str:
    return (
        "✅ Signing in as **%s** using the preset credentials.\n\n"
        "Nothing was stored. Send a screenshot whenever you are ready, or "
        "/login to use different ones." % username
    )


def ask_username() -> str:
    return (
        "🔑 What is your site username?\n\n"
        "This is the name you sign in to the site with — not your Telegram "
        "handle. Send it as a message here."
    )


def bad_username() -> str:
    return (
        "That does not look like a username. The site needs 3–40 characters. "  # noqa: RUF001
        "Try again."
    )


def ask_password() -> str:
    return (
        "🔑 And your site password.\n\n"
        "Send it as a message here and I will delete the message immediately. "
        "A notification may still have shown, so prefer a private chat.\n\n"
        "I store it encrypted, keyed on this bot's token."
    )


def credentials_saved(username: str) -> str:
    return (
        "✅ Credentials saved for **%s**.\n\n"
        "Encrypted on disk, keyed on this bot's token. Rotating the token at "
        "@BotFather makes the stored copy permanently unreadable — which is "
        "the intended response to a suspected compromise.\n\n"
        "Send a screenshot whenever you are ready. /logout to forget." % username
    )


def credential_store_failed(detail: object) -> str:
    return (
        "❌ Could not save the credentials (`%s`).\n\n"
        "Nothing was stored and nothing was recorded. Try /login again." % detail
    )


def need_credentials() -> str:
    return (
        "🔑 I need your site credentials before I can do anything.\n\n"
        "Send /login and I will ask for them. Nothing is recorded until you "
        "confirm a Submission, and I cannot read your steps without them — "
        "the site does the OCR inside the page."
    )


def vault_unreadable(detail: object) -> str:
    return (
        "🔑 The stored credentials could not be read (`%s`).\n\n"
        "This normally means the bot token was rotated, which makes the old "
        "encrypted copy unrecoverable by design.\n\n"
        "Send /login to supply them again." % detail
    )


def no_prompt() -> str:
    return (
        "Nothing to do right now. Send a screenshot, or /login if you want to "
        "change your credentials."
    )


def scrub_failed() -> str:
    return (
        "_I could not delete the message above. It contained a secret — "
        "please delete it yourself._"
    )


def logged_out() -> str:
    return "🔑 Credentials forgotten. Send /login when you need them again."


def welcome(name: str | None) -> str:
    return (
        "👋 Hello%s\n\n"
        "Send a screenshot of your step tracker's day view and I will:\n"
        "1. upload it and report the step count the site reads\n"
        "2. ask you for the activity date (today by default)\n"
        "3. record it once you confirm\n\n"
        "Nothing is recorded until you confirm." % (", %s!" % name if name else "!")
    )


def scanning(name: str) -> str:
    """Announce that the Screenshot arrived.

    `name` is escaped: a Telegram username may contain `_`, which is an italic
    delimiter in Telegram's legacy Markdown, so an unescaped one makes Telegram
    reject the whole message with no local error. Same class of bug as
    `whitelist_claim` taking out /status.
    """
    return "%s Reading **%s**…" % (EMOJI["scan"], md(name))


def ocr_read(steps: int, reported: str, plausible: bool = True) -> str:
    head = "%(ocr)s The site read **%(s)s** steps." % {
        "ocr": EMOJI["ocr"],
        "s": reported,
    }
    if plausible:
        return head
    return (
        "⚠️ The site read **%s** steps, which is outside the range I expect "
        "(%d–%d). I am not going to record that without a look from you."  # noqa: RUF001
        % (reported, 100, 200_000)
    )


def queued(reported: str, position: int) -> str:
    return (
        "%(ocr)s The site read **%(s)s** steps. Queued as #%(position)d.\n\n"
        "Confirm or cancel the current screenshot first — "
        "I will bring this one up next."
        % {"ocr": EMOJI["ocr"], "s": reported, "position": position}
    )


def choose_date(steps: int, reported: str, default_label: str) -> str:
    return (
        "%(date)s Which day do these **%(s)s** steps belong to?\n\n"
        "_Defaults to today. Pick a different date if this is yesterday's "
        "screenshot._"
        % {
            "date": EMOJI["date"],
            "s": reported,
        }
    )


def future_day() -> str:
    return "That day hasn't happened yet — pick today or earlier."


def pre_event_day() -> str:
    return "The event runs October 2026 — pick a day in October."


def confirming(steps: int, reported: str, date_label: str, iso: str) -> str:
    return (
        "%(ok)s Confirming **%(s)s** steps for **%(dl)s**.\n\n"
        "Site read: `%(s)s`\n"
        "Activity date: `%(iso)s`"
        % {
            "ok": EMOJI["ok"],
            "s": reported,
            "dl": date_label,
            "iso": iso,
        }
    )


def overwrite_warning(new_steps: int, old_steps: int, date_label: str) -> str:
    diff = old_steps - new_steps
    return (
        "%(ow)s **You already have %(old)s steps recorded for %(dl)s.**\n"
        "This screenshot reads **%(new)s** — %(diff)s fewer.\n\n"
        "That usually means a blurry or partial screenshot. Recording it would "
        "replace your %(old)s and the old screenshot would be deleted.\n\n"
        "Overwrite anyway?"
        % {
            "ow": EMOJI["overwrite"],
            "old": f"{old_steps:,}",
            "dl": date_label,
            "new": f"{new_steps:,}",
            "diff": f"{diff:,}",
        }
    )


def overwrite_upgrade(new_steps: int, old_steps: int, date_label: str) -> str:
    return "%(ow)s **Update for %(dl)s:** %(old)s → %(new)s steps." % {
        "ow": EMOJI["overwrite"],
        "dl": date_label,
        "old": f"{old_steps:,}",
        "new": f"{new_steps:,}",
    }


def no_steps() -> str:
    return (
        "❌ The site could not read a step count from that image, so I have "
        "not recorded anything.\n\n"
        "A few things that help: the tracker's **day view** (not week or month), "
        "the step total clearly visible, and the full screenshot uncropped."
    )


def implausible(reported: str) -> str:
    return (
        "⚠️ The site read **%s** steps. That is not a plausible daily total, so "
        "I stopped rather than record it.\n\nTry a clearer screenshot of the "
        "day view." % reported
    )


def low_memory(detail: str = "") -> str:
    txt = (
        "⚠️ This host is too low on memory to run the browser right now, so I "
        "have not recorded anything.\n\n"
        "The site's OCR runs inside the page, so the browser is required to "
        "read your steps — there is no lighter path. Nothing has been submitted; "
        "send the screenshot again in a minute."
    )
    if detail:
        txt += "\n\n`%s`" % detail
    return txt


def ocr_changed(was: object, now: object, date_label: str) -> str:
    return (
        "⚠️ **The site's read changed between the two passes.**\n\n"
        "When you confirmed: `%s`\nJust now: `%s`\n\n"
        "I have stopped rather than record a number you did not agree to. "
        "Nothing was saved for %s — send the screenshot again and confirm again."
        % (was, now, date_label)
    )


def site_changed(what: object) -> str:
    return (
        "⚠️ The site did not look the way I expect (%s).\n\n"
        "I have stopped rather than guess — nothing was recorded. This usually "
        "means the page changed and the Relay needs a look." % what
    )


def login_failed(reason: object) -> str:
    return (
        "❌ I could not sign in to the site (%s).\n\n"
        "If the password changed, update `SITE_PASSWORD` in `secrets.env`. "
        "I will not retry on my own." % reason
    )


def recorded(
    reported: str, date_label: str, iso: str, site_text: str | None = None
) -> str:
    txt = (
        "%(ok)s **Recorded %(s)s steps for %(dl)s.**\n\n"
        "The site confirmed the Submission."
        % {
            "ok": EMOJI["ok"],
            "s": reported,
            "dl": date_label,
        }
    )
    if site_text:
        for ln in site_text.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            low = ln.lower()
            if "step" in low or "point" in low or "recorded" in low:
                txt += "\n> %s" % ln
                break
    return txt


def send_a_screenshot() -> str:
    return (
        "Send me a screenshot of your step tracker's **day view** — the one "
        "showing the daily total, not week or month.\n\n"
        "I will read the step count, show it to you, and ask which day it "
        "belongs to before anything is recorded."
    )


def cancel_prompt_first() -> str:
    return (
        "You are part-way through telling me your credentials.\n\n"
        "Finish it, or tap Cancel to stop. Then send the screenshot."
    )


def cancelled() -> str:
    return "\U0001f5d1️ Discarded. Nothing was recorded."


def rank_line(
    total_steps: int | None,
    total_points: int | None,
    house: str | None = None,
    position: str | None = None,
) -> str:
    bits = []
    if house:
        bits.append("House: **%s**" % house)
    if position:
        bits.append(position)
    if total_steps is not None:
        bits.append(f"You: **{total_steps:,}** steps")
    if total_points is not None:
        bits.append("**%s** points" % f"{total_points:,}")
    return "%s %s" % (EMOJI["rank"], "  ·  ".join(bits))


def log_lines(subs: list[dict[str, object]]) -> str:
    if not subs:
        return "%s No Submissions recorded yet." % EMOJI["log"]
    out = [EMOJI["log"] + " **Recent Submissions**", ""]
    for s in subs[:15]:
        out.append(
            "`%s`  **%s** steps   _%s_"
            % (s["activity_date"], "{:,}".format(s["steps"]), s["recorded_at"])
        )
    return "\n".join(out)


# ---------- sync ----------


def syncing() -> str:
    return "%s Reading the site's own record. This submits nothing." % EMOJI["sync"]


def sync_empty() -> str:
    """The site showed no days at all.

    Distinct from a successful sync of zero, which would look the same as a
    site with nothing on it -- and would leave the overwrite guard blind.
    """
    return (
        "%s The site showed no days I could read.\n\n"
        "Nothing was stored, so the overwrite guard is unchanged. "
        "If you have days on the site, the dashboard may have changed -- "
        "send a screenshot and we will see." % EMOJI["sync"]
    )


def sync_report(days: list[tuple[datetime.date, int]], written: int) -> str:
    """What the site holds, and how much of it was new to the bot.

    `days` is [(date, steps), ...] newest first. The count is stated rather
    than left implied, because the site's list is "Recent submissions" and is
    bounded by whatever it chooses to show.
    """
    out = [
        EMOJI["sync"]
        + " **Synced %d day%s from the site**" % (written, "" if written == 1 else "s"),
        "",
    ]
    for day, steps in days[:15]:
        out.append("`%s`  **%s** steps" % (day.strftime("%Y-%m-%d"), f"{steps:,}"))
    out.append("")
    out.append("_Read through the site's own dashboard. Nothing was submitted._")
    return "\n".join(out)
