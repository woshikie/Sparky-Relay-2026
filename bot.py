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

import config
import datepicker
import ledger
import memory
import relay_site
import words
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


class BrowserUnavailable(Exception):
    """The host cannot spare the RAM for the browser right now."""


@contextlib.contextmanager
async def browser_session():
    """Yield a started Relay, then close the browser.

    Transient by design: the browser exists for the duration of one Screenshot
    and is torn down afterwards, so idle RAM is the bot process alone.
    """
    async with _site_lock:
        r = get_relay()
        try:
            rep = await asyncio.to_thread(memory.require_memory)
        except memory.InsufficientMemory as e:
            raise BrowserUnavailable(str(e))
        log_ctx = "%.0fMB free" % rep["available_mb"]
        print("[relay] launching browser (%s)" % log_ctx, flush=True)
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
    r = get_relay()
    if not force and ledger.session_valid():
        log(ctx, "site session still valid per ledger")
    r.start()
    await asyncio.to_thread(r.login, config.SITE_USERNAME, config.SITE_PASSWORD)


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
    if not authorised(ctx, chat.id):
        await msg.reply_text(words.unauthorized(chat.id))
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
            await asyncio.to_thread(r.login, config.SITE_USERNAME,
                                    config.SITE_PASSWORD)
            steps, reported = await asyncio.to_thread(r.upload, path)
    except memory.InsufficientMemory:
        await scratch.delete()
        await msg.reply_text(words.low_memory(), parse_mode=ParseMode.MARKDOWN)
        return
    except BrowserUnavailable as e:
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
        y, m = int(rest[0]), int(rest[1])
        m -= 1
        if m < 1:
            y, m = y - 1, 12
        await q.edit_message_reply_markup(reply_markup=kb_pick_date(y, m))
        await q.answer()
        return
    elif kind == "next":
        y, m = int(rest[0]), int(rest[1])
        m += 1
        if m > 12:
            y, m = y + 1, 1
        await q.edit_message_reply_markup(reply_markup=kb_pick_date(y, m))
        await q.answer()
        return
    elif kind == "day":
        st["date"] = datetime.date.fromisoformat(rest[0])
    elif kind == "none":
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
            await asyncio.to_thread(r.login, config.SITE_USERNAME,
                                    config.SITE_PASSWORD)
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
    except (memory.InsufficientMemory, BrowserUnavailable) as e:
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
    chat = update.effective_chat
    if not authorised(ctx, chat.id):
        await update.effective_message.reply_text(words.unauthorized(chat.id))
        return
    ledger.remember_user(chat.id, getattr(chat, "username", None))
    await update.effective_message.reply_text(
        words.welcome(getattr(chat, "first_name", None)),
        parse_mode=ParseMode.MARKDOWN)


async def on_log(update: Update, ctx):
    chat = update.effective_chat
    if not authorised(ctx, chat.id):
        await update.effective_message.reply_text(words.unauthorized(chat.id))
        return
    await update.effective_message.reply_text(
        words.log_lines(ledger.all_submissions()), parse_mode=ParseMode.MARKDOWN)


async def on_status(update: Update, ctx):
    chat = update.effective_chat
    if not authorised(ctx, chat.id):
        await update.effective_message.reply_text(words.unauthorized(chat.id))
        return
    subs = ledger.all_submissions()
    sess = ledger.load_session()
    until = ""
    if sess and sess.get("expires_at"):
        until = " (expires %s)" % time.strftime(
            "%H:%M:%S", time.localtime(sess["expires_at"]))
    users = ledger.known_users()
    rep = memory.budget_report()
    txt = (
        "**Relay status**\n\n"
        "Site: `%s`\n"
        "User: `%s`\n"
        "Browser: headless=%s, launched per Screenshot\n"
        "Memory: %.0fMB total, %.0fMB available (browser needs ~%dMB) — can "
        "launch: **%s**\n"
        "Session: %s%s\n"
        "Submissions recorded: %d\n"
        "Authorised chats: %s\n"
        "Pending confirmations: %d"
        % (config.SITE_BASE, config.SITE_USERNAME, config.HEADLESS,
           rep["total_mb"], rep["available_mb"], rep["browser_peak_mb"],
           "yes" if rep["can_launch"] else "NO",
           "valid" if ledger.session_valid() else "needs sign-in", until,
           len(subs), ", ".join("`%s`" % u["chat_id"] for u in users) or "none",
           len(PENDING))
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


def authorised(ctx, chat_id):
    if config.ALLOWED_CHAT_ID:
        try:
            return int(config.ALLOWED_CHAT_ID) == int(chat_id)
        except ValueError:
            return False
    ledger.remember_user(chat_id)
    known = [u["chat_id"] for u in ledger.known_users()]
    if not known:
        return True
    return int(chat_id) in known


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
    config.require()
    ledger.init()
    app = (ApplicationBuilder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .post_init(on_ready)
           .build())

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_start))
    app.add_handler(CommandHandler("log", on_log))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CallbackQueryHandler(cb_date, pattern=r"^dt:"))
    app.add_handler(CallbackQueryHandler(cb_ok, pattern=r"^ok:"))

    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(
        filters.Document.MimeType("image/"), on_photo))

    if app.job_queue is None:
        raise SystemExit(
            "job-queue support is missing.\n"
            "  install it:  pip install 'python-telegram-bot[job-queue]'\n"
            "  the nightly ledger backup is scheduled through the job queue.")
    hh, mm = [int(x) for x in config.BACKUP_TIME.split(":")]
    # wallclock, not the time module: `time(hour=..., minute=...)` is a class.
    app.job_queue.run_daily(backup_job, wallclock(hour=hh, minute=mm),
                            name="ledger-backup")

    if not config.ALLOWED_CHAT_ID:
        print("[relay] ALLOWED_CHAT_ID not set: the first chat to message the "
              "bot becomes the authorised chat. Pin it in secrets.env after.")
    print("[relay] %s" % memory.describe())
    if not memory.budget_report()["can_launch"]:
        print("[relay] WARNING: not enough free memory to run the browser. "
              "Screenshots will be refused until memory frees up. "
              "Consider a larger host — see docs/adr/0004.")
    print("[relay] starting")
    try:
        app.run_polling(drop_pending_updates=True, close_loop=False)
    finally:
        get_relay().stop()


async def on_ready(app):
    print("[relay] online as @%s" % (await app.bot.get_me()).username, flush=True)


if __name__ == "__main__":
    main()
