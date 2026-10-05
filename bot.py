#!/usr/bin/env python3
"""The Relay: a Telegram bot that relays a Screenshot into a Submission.

Flow per Screenshot:
  photo -> upload via headless Firefox -> report the site's Detected Steps
        -> ask for the Activity Date (today / yesterday / datepicker)
        -> Confirmation (with an extra warning if this would overwrite a
           higher number already recorded) -> record -> report the site's result
"""
import asyncio
import contextlib
import datetime
import io
import os
import sys
import time
from datetime import time as wallclock

import sys

import errors

# config validates ACCESS_MODE at import time, and everything else imports it.
# Doing it first, in a try, means a bad mode prints the message telling the
# operator which line of secrets.env to fix -- rather than a traceback that
# buries it under import frames.
try:
    import config
except errors.ConfigRefused as _exc:
    sys.stderr.write("%s\n" % _exc)
    sys.exit(2)

import access          # noqa: E402  (must follow the config guard)
import datepicker      # noqa: E402
import ledger          # noqa: E402
import memory          # noqa: E402
import relay_site      # noqa: E402
import vault           # noqa: E402
import words           # noqa: E402
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler)

DBG = bool(os.environ.get("RELAY_DEBUG"))

# Pending Screenshot state, keyed by (chat_id, message_id) of the Confirmation.
PENDING = {}

# Longest edge of the Screenshot we will keep. The site downscales to 1200px
# anyway, and smaller photos upload faster on a 1GB VM.
MAX_EDGE = 1200


def sg_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        hours=config.SGT_OFFSET_HOURS)


def sg_today():
    return sg_now().date()


def log(ctx, msg):
    if DBG and ctx is not None:
        try:
            ctx.job_queue.run_once(
                lambda _c: None, 0)  # no-op; keeps job_queue referenced
        except Exception:
            pass
    print("[relay] %s" % msg, flush=True)


# ----------------------------------------------------------------------
# image intake
# ----------------------------------------------------------------------

async def save_photo(update: Update, ctx) -> str:
    """Download the largest available photo to disk. Returns the path."""
    msg = update.effective_message
    photos = list(msg.photo or [])
    doc = msg.document
    if not photos and not doc:
        return None
    if photos:
        f = photos[-1]
        data = await f.get_file()
    else:
        if (doc.mime_type or "").split("/")[0] != "image":
            return None
        data = await doc.get_file()
    raw = await data.download_as_bytearray()

    from PIL import Image
    im = Image.open(io.BytesIO(bytes(raw)))
    im = im.convert("RGB")
    w, h = im.size
    scale = min(1.0, MAX_EDGE / float(max(w, h)))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)
    outdir = config.INBOX
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "%d-%d.jpg" % (msg.chat_id, msg.message_id))
    im.save(path, "JPEG", quality=88)
    log(ctx, "photo saved %s (%dx%d)" % (os.path.basename(path), *im.size))
    return path


# ----------------------------------------------------------------------
# browser access
# ----------------------------------------------------------------------

# The site's OCR runs in the page, so the browser is the expensive part: ~640MB
# measured. On a 1GB host it must not be held open. One Relay at a time, and
# only for as long as a Screenshot is in flight.
_site_lock = asyncio.Lock()
_relay = None


def get_relay() -> relay_site.Relay:
    global _relay
    if _relay is None:
        _relay = relay_site.Relay(config.SITE_BASE, headless=config.HEADLESS)
    return _relay


class _NoCredentials(Exception):
    """This chat has not supplied Site credentials yet."""


@contextlib.asynccontextmanager
async def browser_session():
    """Yield a started Relay, then close the browser.

    Transient by design: the browser exists for the duration of one Screenshot
    and is torn down afterwards, so idle RAM is the bot process alone.

    This must be an *async* context manager. With the plain
    `contextlib.contextmanager` on an `async def`, every caller doing
    `async with browser_session()` fails at runtime with
    "'_GeneratorContextManager' object does not support the asynchronous
    context manager protocol" -- which is what happened on the first real
    Screenshot, after every test had passed.
    """
    async with _site_lock:
        r = get_relay()
        # InsufficientMemory propagates as itself: it already carries the
        # numbers, and wrapping it in a second exception type meant callers had
        # to catch both for one condition.
        rep = await asyncio.to_thread(memory.require_memory)
        print("[relay] launching browser (%.0fMB free)" % rep["available_mb"],
              flush=True)
        try:
            await asyncio.to_thread(r.start)
            yield r
        finally:
            print("[relay] closing browser", flush=True)
            await asyncio.to_thread(r.stop)
            mem_after = memory.mem_mb()
            if mem_after:
                print("[relay] %.0fMB available after close" % mem_after,
                      flush=True)


async def site_login(ctx, force=False):
    """Sign in using the credentials held for this chat.

    The old ledger "session_valid" shortcut is gone: it was never written to, so
    it always reported stale, and with prompted credentials the browser profile
    is what actually decides whether a re-login is needed.
    """
    raise RuntimeError("site_login(ctx) is superseded by browser_session()")


async def sign_in(chat_id):
    """Sign the Relay in for this chat, or raise something we can explain.

    Called inside an open browser_session(), so the caller owns the browser
    lifetime; this only resolves the credentials and logs in.
    """
    r = get_relay()
    username, password = site_credentials(chat_id)
    await asyncio.to_thread(r.login, username, password)
    return r


# ----------------------------------------------------------------------
# keyboards
# ----------------------------------------------------------------------

def kb_date_default(steps, reported):
    t = sg_today()
    y = t - datetime.timedelta(days=1)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Today (%s)" % t.strftime("%d %b"), callback_data="dt:today"),
        InlineKeyboardButton("Yesterday (%s)" % y.strftime("%d %b"), callback_data="dt:yday"),
    ], [
        InlineKeyboardButton("\U0001F4C5 Open datepicker", callback_data="dt:pick:%d:%d" % (t.year, t.month)),
    ]])


def kb_confirm(steps, reported, iso):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm %s" % reported, callback_data="ok:go"),
    ], [
        InlineKeyboardButton("Change date", callback_data="dt:back"),
        InlineKeyboardButton("\U0001F5D1 Cancel", callback_data="ok:cancel"),
    ]])


def kb_overwrite(new_steps, old_steps, iso):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F504 Overwrite anyway", callback_data="ok:go"),
    ], [
        InlineKeyboardButton("Keep my %s" % "{:,}".format(old_steps), callback_data="ok:cancel"),
    ]])


def kb_pick_date(year, month):
    return datepicker.keyboard(year, month, sg_today())


# ----------------------------------------------------------------------
# the flow
# ----------------------------------------------------------------------

async def on_photo(update: Update, ctx):
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d), parse_mode=ParseMode.MARKDOWN)
        return

    scratch = await msg.reply_text(words.scanning(
        getattr(chat, "username", None) or "your screenshot"),
        parse_mode=ParseMode.MARKDOWN)
    for frame in ("\U0001F5D3️", "\U0001F4F7", "\U0001F4E6", "\U0001F4C5"):
        try:
            await scratch.edit_text("%s Processing…" % frame)
            await asyncio.sleep(0.35)
        except TelegramError:
            pass

    path = await save_photo(update, ctx)
    if not path:
        await scratch.edit_text("That does not look like an image. Send a "
                                "screenshot of your tracker's day view.")
        return

    # Read the number. The browser closes as soon as we have it: on a 1GB host
    # holding ~640MB open while the user decides on a date is not affordable.
    try:
        async with browser_session() as r:
            await sign_in(chat.id)
            steps, reported = await asyncio.to_thread(r.upload, path)
    except _NoCredentials:
        await scratch.delete()
        await msg.reply_text(words.need_credentials(),
                             parse_mode=ParseMode.MARKDOWN)
        await ask_credentials(msg, chat.id)
        return
    except vault.DecryptionFailed as e:
        # Almost always a rotated bot token: the vault key no longer opens the
        # stored entry. The old password is unrecoverable by design.
        await scratch.delete()
        await msg.reply_text(words.vault_unreadable(str(e)),
                             parse_mode=ParseMode.MARKDOWN)
        await ask_credentials(msg, chat.id)
        return
    except memory.InsufficientMemory as e:
        await scratch.delete()
        await msg.reply_text(words.low_memory(str(e)),
                             parse_mode=ParseMode.MARKDOWN)
        return
    except relay_site.NoStepsFound:
        await scratch.delete()
        await msg.reply_text(words.no_steps(), parse_mode=ParseMode.MARKDOWN)
        return
    except relay_site.SiteChanged as e:
        await scratch.delete()
        await msg.reply_text(words.site_changed(str(e)),
                             parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        await scratch.delete()
        await msg.reply_text("❌ Upload failed: `%s`" % str(e)[:200],
                             parse_mode=ParseMode.MARKDOWN)
        log(ctx, "upload error: %r" % (e,))
        return

    plausible = relay_site.MIN_STEPS <= steps <= relay_site.MAX_STEPS
    await scratch.edit_text(words.ocr_read(steps, reported, plausible),
                            parse_mode=ParseMode.MARKDOWN)
    if not plausible:
        await msg.reply_text(words.implausible(reported), parse_mode=ParseMode.MARKDOWN)
        return

    await msg.reply_text(words.choose_date(steps, reported, ""),
                         parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_date_default(steps, reported))

    PENDING[(chat.id, msg.message_id)] = {
        "path": path,
        "steps": steps,
        "reported": reported,
        "scratch": scratch,
        "date": None,
    }


async def cb_date(update: Update, ctx):
    """Every date button routes here, so every one of them must be answered.

    A callback that raises leaves the button spinning in the client for good:
    Telegram gives no error and no reply, which is exactly what "it's stuck"
    looks like. So the whole body runs guarded, and an unexpected failure says
    so rather than hanging. cb_ok already worked this way; cb_date did not,
    and a malformed payload -- a stale grid from before a restart, say --
    reached int() unguarded.
    """
    q = update.callback_query
    try:
        await _cb_date(update, ctx)
    except Exception as e:
        log(ctx, "date callback error: %r" % (e,))
        try:
            await q.answer("something went wrong — send the screenshot again",
                           show_alert=True)
        except TelegramError:
            pass


async def _cb_date(update: Update, ctx):
    q = update.callback_query
    chat = update.effective_chat
    msg = q.message
    if not authorised(ctx, chat.id):
        await q.answer("not authorised", show_alert=True)
        return
    data = q.data or ""
    _, kind, *rest = data.split(":")

    st = PENDING.get((chat.id, msg.reply_to_message.message_id
                      if msg.reply_to_message else None))
    if st is None:
        # attach to the most recent pending for this chat
        cands = [k for k in PENDING if k[0] == chat.id]
        if not cands:
            await q.answer("This request expired — send the screenshot again.",
                           show_alert=True)
            await q.edit_message_text("Expired. Send the screenshot again.")
            return
        st = PENDING[max(cands, key=lambda k: k[1])]

    if kind == "today":
        st["date"] = sg_today()
    elif kind == "yday":
        st["date"] = sg_today() - datetime.timedelta(days=1)
    elif kind == "back":
        await q.edit_message_reply_markup(reply_markup=kb_date_default(
            st["steps"], st["reported"]))
        await q.answer()
        return
    elif kind == "pick":
        y, m = int(rest[0]), int(rest[1])
        await q.edit_message_text("\U0001F4C5 Pick the activity date.",
                                  reply_markup=kb_pick_date(y, m))
        await q.answer()
        return
    elif kind == "prev":
        # The target month arrives already resolved: datepicker.shift() owns the
        # wrap, so there is no January/December arithmetic here to get wrong.
        await q.edit_message_reply_markup(
            reply_markup=kb_pick_date(int(rest[0]), int(rest[1])))
        await q.answer()
        return
    elif kind == "next":
        await q.edit_message_reply_markup(
            reply_markup=kb_pick_date(int(rest[0]), int(rest[1])))
        await q.answer()
        return
    elif kind == "day":
        st["date"] = datetime.date.fromisoformat(rest[0])
    elif kind == "none":
        # Padding in the calendar grid. Answer, so the client stops spinning.
        await q.answer()
        return
    else:
        await q.answer()
        return

    date = st["date"]
    iso = date.isoformat()
    label = "%s %s, %d" % (date.strftime("%B"), ordinal(date.day), date.year)
    st["iso"] = iso
    st["label"] = label

    # The number being confirmed comes from the pending record, not from the
    # callback data: the callback only carries a date. Reading it from the
    # record is also what makes the Confirmation impossible to drift from the
    # number the user approved.
    steps = st.get("steps")
    reported = st.get("reported")

    # Overwrite guard: only the downward direction is challenged.
    prev = ledger.last_submission(iso)
    st["prev"] = prev
    if prev and steps < prev["steps"]:
        await q.edit_message_text(
            words.overwrite_warning(steps, prev["steps"], label),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_overwrite(steps, prev["steps"], iso))
        await q.answer("this would lower your recorded steps", show_alert=True)
        return
    if prev and steps > prev["steps"]:
        await q.edit_message_text(
            words.overwrite_upgrade(steps, prev["steps"], label),
            parse_mode=ParseMode.MARKDOWN)
        await q.answer()

    await q.edit_message_text(
        words.confirming(steps, reported, label, iso),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_confirm(steps, reported, iso))
    await q.answer()


def ordinal(n):
    if 11 <= (n % 100) <= 13:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


async def cb_ok(update: Update, ctx):
    q = update.callback_query
    chat = update.effective_chat
    msg = q.message
    if not authorised(ctx, chat.id):
        await q.answer("not authorised", show_alert=True)
        return
    action = (q.data or "").split(":")[1]

    cands = [k for k in PENDING if k[0] == chat.id]
    if not cands:
        await q.answer("This request expired — send the screenshot again.",
                       show_alert=True)
        await q.edit_message_text("Expired. Send the screenshot again.")
        return
    key = max(cands, key=lambda k: k[1])
    st = PENDING[key]

    if action == "cancel":
        PENDING.pop(key, None)
        await q.edit_message_text(words.cancelled())
        await q.answer()
        return

    date = st.get("date")
    if not date:
        await q.answer("Pick a date first.", show_alert=True)
        return

    iso = st["iso"]
    label = st["label"]
    try:
        await q.edit_message_text(
            "⏳ Recording %s steps for %s…" % (st["reported"], label))
    except TelegramError:
        pass

    # The browser was closed after reading the number, so re-open it, re-upload
    # the same Screenshot, then set the date and Commit in one go. This is a
    # deliberate trade: a second OCR pass costs ~25s of the host's RAM twice
    # instead of holding it once for as long as the user takes to decide.
    site_text = ""
    try:
        async with browser_session() as r:
            await sign_in(chat.id)
            # Re-reading must agree with what the user confirmed, or the
            # Screenshot is not the one they agreed to submit.
            steps2, reported2 = await asyncio.to_thread(r.upload, st["path"])
            if steps2 != st["steps"]:
                PENDING.pop(key, None)
                await msg.reply_text(words.ocr_changed(
                    st["reported"], reported2, label),
                    parse_mode=ParseMode.MARKDOWN)
                await q.answer("the site's read changed", show_alert=True)
                return
            await asyncio.to_thread(r.set_date, date)
            site_text = await asyncio.to_thread(r.commit, st["steps"])
    except _NoCredentials:
        PENDING.pop(key, None)
        await msg.reply_text(words.need_credentials(),
                             parse_mode=ParseMode.MARKDOWN)
        await ask_credentials(msg, chat.id)
        await q.answer("credentials needed", show_alert=True)
        return
    except vault.DecryptionFailed as e:
        PENDING.pop(key, None)
        await msg.reply_text(words.vault_unreadable(str(e)),
                             parse_mode=ParseMode.MARKDOWN)
        await ask_credentials(msg, chat.id)
        await q.answer("stored credentials unreadable", show_alert=True)
        return
    except memory.InsufficientMemory as e:
        PENDING.pop(key, None)
        await msg.reply_text(words.low_memory(str(e) or ""),
                             parse_mode=ParseMode.MARKDOWN)
        await q.answer("not enough memory", show_alert=True)
        return
    except relay_site.NoStepsFound:
        PENDING.pop(key, None)
        await msg.reply_text(words.no_steps(), parse_mode=ParseMode.MARKDOWN)
        await q.answer("the site read nothing this time", show_alert=True)
        return
    except relay_site.SiteChanged as e:
        PENDING.pop(key, None)
        await msg.reply_text(words.site_changed(str(e)),
                             parse_mode=ParseMode.MARKDOWN)
        await q.answer()
        return
    except Exception as e:
        await msg.reply_text(
            "❌ Could not record: `%s`\n\nNothing was saved — send the "
            "screenshot again when ready." % str(e)[:200],
            parse_mode=ParseMode.MARKDOWN)
        await q.answer()
        log(ctx, "commit error: %r" % (e,))
        return

    ledger.record(iso, st["steps"], reported=st["reported"], site_label=label,
                  msg_id=msg.message_id)
    PENDING.pop(key, None)

    try:
        await msg.reply_text(words.recorded(st["reported"], label, iso, site_text),
                             parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass

    # House standing: a second browser launch, best effort, clearly separate
    # from the result. Skipped if memory is short — it is decoration.
    try:
        async with browser_session() as r:
            board = await asyncio.to_thread(r.leaderboard_line)
        if board:
            line = parse_profile(board)
            if line:
                await msg.reply_text(line, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

    try:
        await q.answer("recorded")
    except TelegramError:
        pass


def parse_profile(board_text):
    """Pull the user's own totals out of the /home DOM text, if present."""
    import re
    total_steps = total_points = None
    m = re.search(r"([\d,]+)\s*\n?\s*total steps", board_text, re.I)
    if m:
        total_steps = int(m.group(1).replace(",", ""))
    m = re.search(r"([\d,]+)\s*\n?\s*total points", board_text, re.I)
    if m:
        total_points = int(m.group(1).replace(",", ""))
    house = None
    m = re.search(r"YOUR HOUSE\s*\n+\s*([A-Za-z]+)", board_text)
    if m:
        house = m.group(1)
    if total_steps is None and total_points is None and house is None:
        return ""
    return words.rank_line(total_steps, total_points, house)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------

async def on_start(update: Update, ctx):
    """Entry point. Access Mode decides whether anything else happens.

    Sends exactly one message. The greeting and the Credentials Prompt used to
    be separate replies, which read as the bot talking to itself; the prompt now
    rides along on the same message, with the reply keyboard attached so the
    commands are one tap away.
    """
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))

    if not d:
        if d.why == "needs_claim":
            # First-run claim: whoever got here first owns the Relay.
            if access.claim(chat.id):
                await msg.reply_text(words.claimed(),
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=kb_reply())
            else:
                await msg.reply_text(words.already_claimed(),
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=kb_reply())
            return
        if d.why == "needs_secret":
            await msg.reply_text(words.secret_prompt(),
                                 parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=kb_reply())
            return
        await msg.reply_text(words.access_refused(d),
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_reply())
        return

    if has_credentials(chat.id):
        body = words.welcome(getattr(chat, "first_name", None))
    else:
        # One message: the greeting, then the prompt. The keyboard below it is
        # whatever this chat needs *now*, so the commands are never more than
        # one tap away and the prompt is answered by tapping, not typing.
        body = words.welcome(getattr(chat, "first_name", None)) + "\n\n" + \
            _credential_prompt_body()
        _start_credential_stage(chat.id)
    await msg.reply_text(body, parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_for(chat.id))


def _credential_prompt_body():
    """The prompt text, without choosing a keyboard for it."""
    if config.has_preset_credentials():
        return words.choose_preset(config.SITE_USERNAME)
    return words.ask_username()


def _start_credential_stage(chat_id):
    if config.has_preset_credentials():
        _set_stage(chat_id, "choose_preset")
    else:
        _set_stage(chat_id, "username")


def has_credentials(chat_id):
    """True if this chat can sign in right now."""
    st = _prompt_stage(chat_id) or {}
    if st.get("preset"):
        return config.has_preset_credentials()
    return ledger.credentials_stored(chat_id) is not None


async def on_login(update: Update, ctx):
    """(Re-)supply Site credentials at any time."""
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d),
                             parse_mode=ParseMode.MARKDOWN)
        return
    _clear_stage(chat.id)
    await ask_credentials(msg, chat.id)


async def on_logout(update: Update, ctx):
    """Drop the stored credentials and any prompt in progress."""
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d),
                             parse_mode=ParseMode.MARKDOWN)
        return
    _clear_stage(chat.id)
    ledger.forget_credentials(chat.id)
    await msg.reply_text(words.logged_out(), parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_for(chat.id))


async def on_log(update: Update, ctx):
    chat = update.effective_chat
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await update.effective_message.reply_text(words.access_refused(d),
                                                  parse_mode=ParseMode.MARKDOWN)
        return
    await update.effective_message.reply_text(
        words.log_lines(ledger.all_submissions()), parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for(chat.id))


async def on_status(update: Update, ctx):
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d), parse_mode=ParseMode.MARKDOWN)
        return
    subs = ledger.all_submissions()
    rep = memory.budget()
    creds = ledger.credentials_stored(chat.id)
    st = _prompt_stage(chat.id) or {}
    preset_in_use = bool(st.get("preset")) and config.has_preset_credentials()

    if preset_in_use:
        who = "%s (preset from config)" % words.code(config.SITE_USERNAME)
    elif creds:
        who = "%s (stored, encrypted)" % words.code(creds["username"])
    else:
        who = "none — send /login"

    granted = ledger.all_access()
    denied = ledger.all_denied()
    txt = (
        "**Relay status**\n\n"
        "**Site:** %s\n"
        "**Signing in as:** %s\n"
        # access.describe() carries its own ** markers, and nesting those inside
        # another ** pair made the whole reply unparseable. Plain fragment.
        "**Access:** %s\n"
        "**Browser:** headless=%s, launched per Screenshot\n"
        "**Memory:** %.0fMB usable (browser needs ~%dMB) — can launch: **%s**\n"
        "**Submissions recorded:** %d\n"
        "**Chats with access:** %s\n"
        "**Chats denied:** %s\n"
        "**Pending confirmations:** %d"
        % (words.code(config.SITE_BASE), who,
           access.describe(markdown=False), config.HEADLESS,
           rep["available_mb"], rep["browser_peak_mb"],
           "yes" if rep["can_launch"] else "NO", len(subs),
           ", ".join("%s (%s)" % (words.code(a["chat_id"]),
                                  words.md(a["how"])) for a in granted) or "none",
           ", ".join(words.code(a["chat_id"]) for a in denied) or "none",
           len(PENDING))
    )
    # Refresh the keyboard so the buttons Telegram is showing match what the
    # bot can actually do right now.
    await msg.reply_text(txt, parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_for(chat.id))


def authorised(ctx, chat_id):
    """Deprecated shim. Access now lives in access.check().

    Kept so nothing calls a removed function, but every handler should ask
    access.check() directly — it returns a reason, which the user needs to be
    told.
    """
    return bool(access.check(chat_id))


# ----------------------------------------------------------------------
# Credentials Prompt
# ----------------------------------------------------------------------

# Where a chat is in the Credentials Prompt, or the Shared Secret exchange.
# chat_id -> {"stage": ..., "preset": bool}
PROMPTING = {}

# How long a half-finished prompt is worth holding before forgetting it.
PROMPT_TTL = 600.0

# Button labels. Centralised because a reply-keyboard button is just text: if
# the label in the keyboard and the label in the dispatch table drift apart, the
# button silently does nothing. tests/test_reply_keyboard.py asserts both
# directions of that mapping.
LABEL_SUBMIT = "📸 Submit steps"
LABEL_LOGIN = "🔑 Sign in"
LABEL_LOGOUT = "🚪 Sign out"
LABEL_STATUS = "📋 Status"
LABEL_LOG = "📜 History"
LABEL_HELP = "❓ Help"
LABEL_CANCEL = "❌ Cancel"
LABEL_NEW_CREDS = "✏️ Different account"


def label_use_preset():
    """'Use <Original Author's username>' — the username is part of the button, so it has to come
    from config rather than being hard-coded here."""
    return "✅ Use %s" % config.SITE_USERNAME


# Everything reachable by tapping, resolved fresh because one label is dynamic.
BUTTON_COMMANDS = {
    LABEL_SUBMIT: "submit",
    LABEL_LOGIN: "login",
    LABEL_LOGOUT: "logout",
    LABEL_STATUS: "status",
    LABEL_LOG: "log",
    LABEL_HELP: "help",
    LABEL_CANCEL: "cancel",
    LABEL_NEW_CREDS: "new_creds",
}


def button_actions():
    """The dispatch table, including the label that depends on config."""
    table = dict(BUTTON_COMMANDS)
    if config.has_preset_credentials():
        table[label_use_preset()] = "use_preset"
    return table


async def _run_button(ctx, msg, chat, action, stage):
    """Dispatch a tapped reply-keyboard button."""
    if action == "submit":
        if stage and stage.get("stage") in ("username", "password"):
            await msg.reply_text(
                words.cancel_prompt_first(), parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_for(chat.id))
            return
        await msg.reply_text(words.send_a_screenshot(),
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_for(chat.id))
        return

    if action == "use_preset":
        _clear_stage(chat.id)
        _set_stage(chat.id, "ready", username=config.SITE_USERNAME, preset=True)
        await msg.reply_text(words.using_preset(config.SITE_USERNAME),
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_for(chat.id))
        return

    if action == "new_creds":
        _set_stage(chat.id, "username")
        await msg.reply_text(words.ask_username(), parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_for(chat.id))
        return

    if action == "cancel":
        _clear_stage(chat.id)
        await msg.reply_text(words.cancelled(), parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_for(chat.id))
        return

    handler = {
        "login": on_login, "logout": on_logout,
        "status": on_status, "log": on_log, "help": on_start,
    }.get(action)
    if handler is None:
        return
    # Re-enter through the command handlers so there is one implementation of
    # each, not a second copy here that could fall behind.
    await handler(_as_update(msg, chat), ctx)
    # A tapped command should leave the keyboard showing what comes next,
    # which for /login is the prompt rather than the standing commands.
    if stage is None:
        pending = _prompt_stage(chat.id)
        if pending and pending.get("stage") in ("username", "password"):
            try:
                await msg.reply_text(words.ask_password()
                                     if pending.get("stage") == "password"
                                     else words.ask_username(),
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=kb_for(chat.id))
            except TelegramError:
                pass


def _as_update(msg, chat):
    update = Update(0, message=msg)
    update._effective_chat = chat
    update._effective_user = chat
    return update


def _prompt_stage(chat_id):
    st = PROMPTING.get(chat_id)
    if st and time.time() - st.get("at", 0) > PROMPT_TTL:
        PROMPTING.pop(chat_id, None)
        return None
    return st


def _set_stage(chat_id, stage, **kw):
    PROMPTING[chat_id] = dict(stage=stage, at=time.time(), **kw)
    return PROMPTING[chat_id]


def _clear_stage(chat_id):
    return PROMPTING.pop(chat_id, None)


async def ask_credentials(msg, chat_id):
    """Offer Preset Credentials if they exist, otherwise just ask."""
    if config.has_preset_credentials():
        _set_stage(chat_id, "choose_preset")
        await msg.reply_text(
            words.choose_preset(config.SITE_USERNAME),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_for(chat_id))
        return
    await ask_username(msg, chat_id)


async def ask_username(msg, chat_id):
    _set_stage(chat_id, "username")
    await msg.reply_text(words.ask_username(), parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_for(chat_id))


async def ask_password(msg, chat_id, username=None, preset=False):
    """Move to the password step, carrying the username forward.

    The username has to survive the transition: it arrived in the previous
    message and exists nowhere else by the time the password lands. _set_stage
    replaces the whole dict rather than merging, so it is passed explicitly --
    an earlier version dropped it here and the password could not be stored.
    """
    _set_stage(chat_id, "password", username=username, preset=preset)
    await msg.reply_text(words.ask_password(), parse_mode=ParseMode.MARKDOWN,
                         reply_markup=kb_for(chat_id))


def kb_done():
    """Back to the standing commands once a prompt is finished."""
    return kb_reply()


def kb_reply():
    """The standing commands, as ordinary buttons above the input box.

    A reply keyboard rather than inline, for two reasons: they are standing
    commands rather than answers to a question, and a reply keyboard stays
    put, so nothing has to be memorised or re-tapped from a scrolled-away
    message.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_SUBMIT), KeyboardButton(LABEL_LOGIN)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_LOG)],
            [KeyboardButton(LABEL_LOGOUT), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,     # survives the client being restarted
        input_field_placeholder="Send a screenshot, or pick one",
    )


def kb_credential_choice():
    """The preset-or-not question, also as a reply keyboard.

    It was inline, which meant the only visible buttons were those two and the
    standing commands were nowhere to be seen. Inline buttons live on one
    message and scroll away; reply buttons stay. Everything that is a standing
    choice lives here; only the submission flow -- the date picker and the
    confirmation -- uses inline, because those belong to one particular step.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(label_use_preset())],
            [KeyboardButton(LABEL_NEW_CREDS), KeyboardButton(LABEL_CANCEL)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Pick one, or type your username",
    )


def kb_prompt():
    """Mid-prompt: the answer has to be typed, so keep the keyboard minimal."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_CANCEL)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Type your answer",
    )


def kb_for(chat_id):
    """The keyboard this chat needs right now."""
    stage = _prompt_stage(chat_id)
    if not stage:
        return kb_reply()
    name = stage.get("stage")
    if name == "choose_preset":
        return kb_credential_choice()
    if name in ("username", "password"):
        return kb_prompt()
    return kb_reply()


async def on_credential_choice(update: Update, ctx):
    """Callback path for the preset choice.

    The choice is now a reply keyboard, so this is only reachable from the
    `kb_after_login` escape hatch on an older message still sitting in the chat.
    Kept, because Telegram keeps inline buttons alive on old messages, and a tap
    that did nothing would be worse than a slightly redundant handler.
    """
    q = update.callback_query
    chat = update.effective_chat
    st = _prompt_stage(chat.id)
    if not st or st.get("stage") != "choose_preset":
        await q.answer("Nothing to choose — tap Sign in first.", show_alert=True)
        return
    if q.data.endswith("preset"):
        # Credentials came from the environment: nothing to store, and the
        # vault is not involved at all.
        _clear_stage(chat.id)
        _set_stage(chat.id, "ready", username=config.SITE_USERNAME, preset=True)
        await q.edit_message_text(words.using_preset(config.SITE_USERNAME),
                                  parse_mode=ParseMode.MARKDOWN)
        await q.message.reply_text(words.send_a_screenshot(),
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=kb_for(chat.id))
        await q.answer("using preset credentials")
    else:
        await q.edit_message_reply_markup(reply_markup=None)
        await ask_username(q.message, chat.id)
        await q.answer()


def kb_after_login():
    """Escape hatch on the standing keyboard, for reaching a different account."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_NEW_CREDS), KeyboardButton(LABEL_LOGOUT)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Send a screenshot, or pick one",
    )


async def on_text(update: Update, ctx):
    """Handle the Credentials Prompt, the Shared Secret exchange, and buttons.

    Reply-keyboard buttons arrive here as ordinary text, so they are dispatched
    first. That has to happen before the prompt handling: mid-prompt the bot
    expects a username or a password, and a tapped "Submit steps" button must
    not be mistaken for either.
    """
    chat = update.effective_chat
    msg = update.effective_message
    text = (msg.text or "").strip()
    st = _prompt_stage(chat.id)

    if text in button_actions():
        await _run_button(ctx, msg, chat, button_actions()[text], st)
        return

    # --- Access gate first: an unauthorised chat gets nothing, and its input
    # --- is never treated as a secret.
    d = access.check(chat.id, getattr(chat, "username", None))
    if d.why in ("needs_secret", "secret_throttled"):
        if not text:
            return
        if d.why == "secret_throttled":
            # Still treat this as a secret attempt, so the message carrying it
            # gets deleted, then say why it did not work. Falling through to
            # the "nothing to do" reply instead would leave a password sitting
            # in the chat with no explanation.
            await _scrub(msg)
            await msg.reply_text(words.secret_throttled(d.retry_after),
                                 parse_mode=ParseMode.MARKDOWN)
            return
        # The rate limit is enforced inside present_secret, not here.
        r = access.present_secret(chat.id, text)
        # The message carried a secret: remove it from the chat immediately.
        await _scrub(msg)
        if r:
            await msg.reply_text(words.secret_accepted(r.how),
                                 parse_mode=ParseMode.MARKDOWN)
            await ask_credentials(msg, chat.id)
        elif r.why == "secret_throttled":
            await msg.reply_text(words.secret_throttled(r.retry_after),
                                 parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.reply_text(words.secret_rejected(),
                                 parse_mode=ParseMode.MARKDOWN)
        return

    if not st:
        # No prompt in progress and access is fine: treat as an unknown
        # command rather than silently swallowing it.
        await msg.reply_text(words.no_prompt(), parse_mode=ParseMode.MARKDOWN)
        return

    if st.get("stage") == "username":
        if not text:
            return
        if len(text) < 3 or len(text) > 40:
            await msg.reply_text(words.bad_username(), parse_mode=ParseMode.MARKDOWN)
            return
        await ask_password(msg, chat.id, username=text, preset=st.get("preset"))
        return

    if st.get("stage") == "password":
        if not text:
            return
        username = st.get("username")
        # Store encrypted, then drop it. The plaintext is not kept anywhere.
        try:
            ledger.save_credentials(chat.id, username, text,
                                    config.TELEGRAM_BOT_TOKEN,
                                    preset=False)
        except Exception as e:
            await msg.reply_text(words.credential_store_failed(str(e)[:120]),
                                 parse_mode=ParseMode.MARKDOWN)
            return
        finally:
            _clear_stage(chat.id)
        await _scrub(msg)
        _set_stage(chat.id, "ready", username=username, preset=False)
        await msg.reply_text(words.credentials_saved(username),
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_after_login())
        return


async def _scrub(msg):
    """Delete a message that carried a secret. Best effort.

    A bot can delete messages in a chat it administers, which covers private
    chats. It cannot undo a notification that already rendered, so the README
    says to use a private chat for anything sensitive.
    """
    try:
        await msg.delete()
    except TelegramError:
        try:
            await msg.reply_text(words.scrub_failed(),
                                 parse_mode=ParseMode.MARKDOWN)
            await msg.delete()
        except TelegramError:
            pass


def site_credentials(chat_id):
    """(username, password) for a chat.

    Raises _NoCredentials when the chat has not supplied any, and
    vault.DecryptionFailed when the stored copy cannot be opened (normally
    because the bot token was rotated). Those are different failures and get
    different replies: "send /login" versus "your stored password is gone,
    send it again".

    Returning None here instead would surface as a TypeError from the tuple
    unpacking in sign_in, which is not a message anyone can act on.
    """
    st = _prompt_stage(chat_id) or {}
    if st.get("preset"):
        if config.has_preset_credentials():
            return config.SITE_USERNAME, config.SITE_PASSWORD
        raise vault.DecryptionFailed(
            "preset credentials are no longer configured")
    creds = ledger.load_credentials(chat_id, config.TELEGRAM_BOT_TOKEN)
    if not creds:
        raise _NoCredentials("no credentials stored for this chat")
    return creds


# ----------------------------------------------------------------------
# scheduled
# ----------------------------------------------------------------------

async def backup_job(ctx):
    """Nightly ledger backup, delivered to Telegram itself."""
    chat_ids = [u["chat_id"] for u in ledger.known_users()]
    if not chat_ids:
        return
    ledger.init()
    with open(ledger.DB, "rb") as f:
        data = f.read()
    stamp = sg_now().strftime("%Y-%m-%d")
    fname = "relay-ledger-%s.sqlite3" % stamp
    await ctx.bot.send_document(
        chat_id=chat_ids[0], document=io.BytesIO(data), filename=fname,
        caption="%s Ledger backup — %d Submission(s)." % (
            words.EMOJI["backup"],
            len(ledger.all_submissions())))


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    try:
        config.require()
    except errors.ConfigRefused as e:
        # A plain message and a non-zero exit, not a traceback.
        sys.stderr.write("%s\n" % e)
        raise SystemExit(2)
    ledger.init()
    app = (ApplicationBuilder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .post_init(on_ready)
           .build())

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_start))
    app.add_handler(CommandHandler("login", on_login))
    app.add_handler(CommandHandler("logout", on_logout))
    app.add_handler(CommandHandler("log", on_log))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CallbackQueryHandler(cb_date, pattern=r"^dt:"))
    app.add_handler(CallbackQueryHandler(cb_ok, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_credential_choice, pattern=r"^cred:"))

    from telegram.ext import MessageHandler, filters
    # Photos first, then text. A photo is never an answer to the Credentials
    # Prompt, and text has to reach on_text even though it is the catch-all.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(
        filters.Document.MimeType("image/"), on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue is None:
        raise SystemExit(
            "job-queue support is missing.\n"
            "  install it:  pip install 'python-telegram-bot[job-queue]'\n"
            "  the nightly ledger backup is scheduled through the job queue.")
    hh, mm = [int(x) for x in config.BACKUP_TIME.split(":")]
    # wallclock, not the time module: `time(hour=..., minute=...)` is a class.
    app.job_queue.run_daily(backup_job, wallclock(hour=hh, minute=mm),
                            name="ledger-backup")

    loaded, total = ledger.init_secrets_from_env(config.SHARED_SECRETS)
    if loaded:
        print("[relay] loaded %d Shared Secret(s) from the environment" % total,
              flush=True)
    print("[relay] %s" % access.describe())
    if config.ACCESS_MODE == "blacklist":
        print("[relay] WARNING: ACCESS_MODE=blacklist — any chat that finds "
              "this bot may drive it. See docs/adr/0006.", flush=True)
    if config.has_preset_credentials():
        print("[relay] preset credentials configured for %s; the bot will "
              "offer them at the Credentials Prompt" % config.SITE_USERNAME,
              flush=True)
    else:
        print("[relay] no preset credentials — you will be asked for a "
              "username and password", flush=True)
    print("[relay] %s" % memory.describe())
    if not memory.budget()["can_launch"]:
        print("[relay] WARNING: not enough free memory to run the browser. "
              "Screenshots will be refused until memory frees up. "
              "Consider a larger host — see docs/adr/0004.")
    print("[relay] starting")
    try:
        # Do NOT drop pending updates. A restart on a small host is routine, and
        # dropping means a "/start" sent during the restart vanishes with no
        # reply and no error -- which looks exactly like the bot being broken.
        # A stale Confirm button from before the restart is refused as expired,
        # which is right anyway: the pending request did not survive.
        app.run_polling(drop_pending_updates=False, close_loop=False)
    finally:
        get_relay().stop()


async def on_ready(app):
    print("[relay] online as @%s" % (await app.bot.get_me()).username, flush=True)


if __name__ == "__main__":
    main()
