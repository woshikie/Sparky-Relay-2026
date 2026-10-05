"""Telegram chat copy. Kept apart so wording changes are one file."""
import datetime

EMOJI = {
    "start": "\U0001F3AF",
    "scan": "\U0001F50D",
    "ocr": "\U0001F9E0",
    "date": "\U0001F4C5",
    "ok": "✅",
    "no": "❌",
    "warn": "⚠️",
    "overwrite": "\U0001F504",
    "rank": "\U0001F3C5",
    "backup": "\U0001F4BE",
    "log": "\U0001F4DC",
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

def claimed():
    return (
        "🔐 **This chat now owns the Relay.**\n\n"
        "Only chats listed in `/status` can drive it. Remove a chat from the "
        "list to revoke its access.\n\n" + HELP
    )


def already_claimed():
    return (
        "🔐 The Relay has already been claimed by another chat.\n\n"
        "Access Mode is `whitelist_claim`, so only the first chat to `/start` "
        "gets in. If that should be you, revoke the existing claim first — "
        "or switch `ACCESS_MODE` in `secrets.env`."
    )


def secret_prompt():
    return (
        "🔑 This Relay needs a Shared Secret.\n\n"
        "Send it here and I will remember this chat. I delete the message as "
        "soon as I read it, but a notification may already have shown — so "
        "send it in a private chat, not a group.\n\n"
        "Wrong guesses are rate limited, not locked out: nobody can lock you "
        "out of your own bot this way."
    )


def secret_accepted(how):
    label = (how or "").split(":", 1)[-1] or "a configured secret"
    return "🔑 Secret accepted (`%s`). This chat now has access." % label


def secret_rejected():
    return (
        "❌ That is not a valid Shared Secret.\n\n"
        "I have deleted the message. Try again, or wait a moment if you are "
        "being rate limited."
    )


def secret_throttled(wait):
    return (
        "⏳ Too many attempts. Try again in %d second%s.\n\n"
        "This is a rate limit, not a lockout — you cannot be locked out."
        % (wait, "" if wait == 1 else "s")
    )


def access_refused(decision):
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


def _chat_id_of(decision):
    return getattr(decision, "chat_id", "unknown")


def _mode_or_unknown():
    import config
    return config.ACCESS_MODE


# --- credentials prompt -------------------------------------------------

def choose_preset(username):
    return (
        "🔑 I have preset credentials for **%s**.\n\n"
        "Use those, or give me different ones? The password is never shown — "
        "I am only telling you which account this would be."
        % username
    )


def using_preset(username):
    return (
        "✅ Signing in as **%s** using the preset credentials.\n\n"
        "Nothing was stored. Send a screenshot whenever you are ready, or "
        "/login to use different ones." % username
    )


def ask_username():
    return (
        "🔑 What is your site username?\n\n"
        "This is the name you sign in to the site with — not your Telegram "
        "handle. Send it as a message here."
    )


def bad_username():
    return (
        "That does not look like a username. The site needs 3–40 characters. "
        "Try again."
    )


def ask_password():
    return (
        "🔑 And your site password.\n\n"
        "Send it as a message here and I will delete the message immediately. "
        "A notification may still have shown, so prefer a private chat.\n\n"
        "I store it encrypted, keyed on this bot's token."
    )


def credentials_saved(username):
    return (
        "✅ Credentials saved for **%s**.\n\n"
        "Encrypted on disk, keyed on this bot's token. Rotating the token at "
        "@BotFather makes the stored copy permanently unreadable — which is "
        "the intended response to a suspected compromise.\n\n"
        "Send a screenshot whenever you are ready. /logout to forget."
        % username
    )


def credential_store_failed(detail):
    return (
        "❌ Could not save the credentials (`%s`).\n\n"
        "Nothing was stored and nothing was recorded. Try /login again."
        % detail
    )


def need_credentials():
    return (
        "🔑 I need your site credentials before I can do anything.\n\n"
        "Send /login and I will ask for them. Nothing is recorded until you "
        "confirm a Submission, and I cannot read your steps without them — "
        "the site does the OCR inside the page."
    )


def vault_unreadable(detail):
    return (
        "🔑 The stored credentials could not be read (`%s`).\n\n"
        "This normally means the bot token was rotated, which makes the old "
        "encrypted copy unrecoverable by design.\n\n"
        "Send /login to supply them again."
        % detail
    )


def no_prompt():
    return (
        "Nothing to do right now. Send a screenshot, or /login if you want to "
        "change your credentials."
    )


def scrub_failed():
    return "_I could not delete the message above. It contained a secret — " \
           "please delete it yourself._"


def logged_out():
    return (
        "🔑 Credentials forgotten. Send /login when you need them again."
    )


def welcome(name):
    return (
        "👋 Hello %s.\n\n"
        "Send a screenshot of your step tracker's day view and I will:\n"
        "1. upload it and report the step count the site reads\n"
        "2. ask you for the activity date (today by default)\n"
        "3. record it once you confirm\n\n"
        "Nothing is recorded until you confirm."
        % {"name": name or "there"}
    )


def scanning(name):
    return "%s Reading **%s**…" % (EMOJI["scan"], name)


def ocr_read(steps, reported, plausible=True):
    head = "%(ocr)s The site read **%(s)s** steps." % dict(
        ocr=EMOJI["ocr"], s=reported)
    if plausible:
        return head
    return (
        "⚠️ The site read **%s** steps, which is outside the range I expect "
        "(%d–%d). I am not going to record that without a look from you."
        % (reported, 100, 200_000)
    )


def choose_date(steps, reported, default_label):
    return (
        "%(date)s Which day do these **%(s)s** steps belong to?\n\n"
        "_Defaults to today. Pick a different date if this is yesterday's "
        "screenshot._" % dict(date=EMOJI["date"], s=reported)
    )


def confirming(steps, reported, date_label, iso):
    return (
        "%(ok)s Confirming **%(s)s** steps for **%(dl)s**.\n\n"
        "Site read: `%(s)s`\n"
        "Activity date: `%(iso)s`"
        % dict(ok=EMOJI["ok"], s=reported, dl=date_label, iso=iso)
    )


def overwrite_warning(new_steps, old_steps, date_label):
    diff = old_steps - new_steps
    return (
        "%(ow)s **You already have %(old)s steps recorded for %(dl)s.**\n"
        "This screenshot reads **%(new)s** — %(diff)s fewer.\n\n"
        "That usually means a blurry or partial screenshot. Recording it would "
        "replace your %(old)s and the old screenshot would be deleted.\n\n"
        "Overwrite anyway?"
        % dict(ow=EMOJI["overwrite"], old="{:,}".format(old_steps),
               dl=date_label, new="{:,}".format(new_steps),
               diff="{:,}".format(diff))
    )


def overwrite_upgrade(new_steps, old_steps, date_label):
    return (
        "%(ow)s **Update for %(dl)s:** %(old)s → %(new)s steps."
        % dict(ow=EMOJI["overwrite"], dl=date_label,
               old="{:,}".format(old_steps), new="{:,}".format(new_steps))
    )


def no_steps():
    return (
        "❌ The site could not read a step count from that image, so I have "
        "not recorded anything.\n\n"
        "A few things that help: the tracker's **day view** (not week or month), "
        "the step total clearly visible, and the full screenshot uncropped."
    )


def implausible(reported):
    return (
        "⚠️ The site read **%s** steps. That is not a plausible daily total, so "
        "I stopped rather than record it.\n\nTry a clearer screenshot of the "
        "day view." % reported
    )


def low_memory(detail=""):
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


def ocr_changed(was, now, date_label):
    return (
        "⚠️ **The site's read changed between the two passes.**\n\n"
        "When you confirmed: `%s`\nJust now: `%s`\n\n"
        "I have stopped rather than record a number you did not agree to. "
        "Nothing was saved for %s — send the screenshot again and confirm again."
        % (was, now, date_label)
    )


def site_changed(what):
    return (
        "⚠️ The site did not look the way I expect (%s).\n\n"
        "I have stopped rather than guess — nothing was recorded. This usually "
        "means the page changed and the Relay needs a look." % what
    )


def login_failed(reason):
    return (
        "❌ I could not sign in to the site (%s).\n\n"
        "If the password changed, update `SITE_PASSWORD` in `secrets.env`. "
        "I will not retry on my own." % reason
    )


def recorded(reported, date_label, iso, site_text=None):
    txt = (
        "%(ok)s **Recorded %(s)s steps for %(dl)s.**\n\n"
        "The site confirmed the Submission." % dict(
            ok=EMOJI["ok"], s=reported, dl=date_label)
    )
    if site_text:
        for ln in site_text.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            if "step" in ln.lower() or "point" in ln.lower() or "recorded" in ln.lower():
                txt += "\n> %s" % ln
                break
    return txt


def cancelled():
    return "\U0001F5D1️ Discarded. Nothing was recorded."


def rank_line(total_steps, total_points, house=None, position=None):
    bits = []
    if house:
        bits.append("House: **%s**" % house)
    if position:
        bits.append(position)
    if total_steps is not None:
        bits.append("You: **{:,}** steps".format(total_steps))
    if total_points is not None:
        bits.append("**%s** points" % "{:,}".format(total_points))
    return "%s %s" % (EMOJI["rank"], "  ·  ".join(bits))


def unauthorized(chat_id):
    return (
        "⛔ This chat is not authorised to use the Relay.\n\n"
        "Chat id: `%s`\n\n"
        "Add it to `ALLOWED_CHAT_ID` in `secrets.env` to allow it."
        % chat_id
    )


def log_lines(subs):
    if not subs:
        return "%s No Submissions recorded yet." % EMOJI["log"]
    out = [EMOJI["log"] + " **Recent Submissions**", ""]
    for s in subs[:15]:
        out.append("`%s`  **%s** steps   _%s_" % (
            s["activity_date"], "{:,}".format(s["steps"]), s["recorded_at"]))
    return "\n".join(out)
