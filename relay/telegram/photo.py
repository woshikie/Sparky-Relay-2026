"""Screenshot intake: saving the photo, reading the Detected Steps.

The read phase holds the browser briefly (HOLD_BROWSER_SECS) for the coming
Confirm: holding ~640MB open indefinitely while the user decides on a date is
not affordable on a 1GB host, but a bounded hold beats a ~20s relaunch. The
two reads (intake and commit) must still agree.
"""

import asyncio
import io
import os

from PIL import Image
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import relay.telegram.access as access
import relay.telegram.failures as failures
import relay.telegram.pending as pending_mod
import relay.telegram.session as session_mod
import relay.telegram.words as words
from relay import config
from relay import progress as progress_mod
from relay.site import driver as relay_site
from relay.telegram.prompts import ask_credentials, present_confirmation, refuse

# Longest edge of the Screenshot we will keep. The site downscales to 1200px
# anyway, and smaller photos upload faster on a 1GB VM.
MAX_EDGE = 1200


# ----------------------------------------------------------------------
# image intake
# ----------------------------------------------------------------------


async def save_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Download the largest available photo to disk. Returns the path."""
    msg = update.effective_message
    assert msg is not None
    photos = list(msg.photo or [])
    doc = msg.document
    if photos:
        data = await photos[-1].get_file()
    elif doc is not None:
        if (doc.mime_type or "").split("/")[0] != "image":
            return None
        data = await doc.get_file()
    else:
        return None
    raw = await data.download_as_bytearray()

    im: Image.Image = Image.open(io.BytesIO(bytes(raw)))
    im = im.convert("RGB")
    w, h = im.size
    scale = min(1.0, MAX_EDGE / float(max(w, h)))
    if scale < 1.0:
        im = im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    outdir = config.INBOX
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "%d-%d.jpg" % (msg.chat_id, msg.message_id))
    im.save(path, "JPEG", quality=88)
    session_mod.log(ctx, "photo saved %s (%dx%d)" % (os.path.basename(path), *im.size))
    return path


# ----------------------------------------------------------------------


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    assert chat is not None and msg is not None
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await refuse(msg, d)
        return

    # One message, edited as the work happens. It used to be a fixed four-frame
    # animation that finished in 1.4s -- before the browser had even launched --
    # so the long part, which is all of it, happened in silence.
    scratch = await msg.reply_text(
        words.scanning(getattr(chat, "username", None) or "your screenshot"),
        parse_mode=ParseMode.MARKDOWN,
    )
    prog = await progress_mod.Progress(scratch).start()

    path = await save_photo(update, ctx)
    if not path:
        await prog.close(
            "That does not look like an image. Send a "
            "screenshot of your tracker's day view."
        )
        return

    # Read the number. The browser is held briefly for the coming Confirm
    # (HOLD_BROWSER_SECS); the commit reuses it instead of relaunching.
    try:
        async with session_mod.browser_session(chat.id, prog, hold=True) as r:
            steps, reported = await asyncio.to_thread(r.upload, path)
    except Exception as e:
        await prog.stop()
        await scratch.delete()
        _alert, _alarm, log_line = await failures.explain(
            msg,
            e,
            operation="Upload",
            start_prompt=lambda: ask_credentials(msg, chat.id),
        )
        if log_line:
            session_mod.log(ctx, log_line)
        return

    plausible = relay_site.MIN_STEPS <= steps <= relay_site.MAX_STEPS
    # The last edit is the one the user reads, so it is never throttled.
    # Implausible reads are refused before the lock: they are never enqueued,
    # on either path.
    await prog.close(
        words.ocr_read(steps, reported, plausible), parse_mode=ParseMode.MARKDOWN
    )
    if not plausible:
        await msg.reply_text(words.implausible(reported), parse_mode=ParseMode.MARKDOWN)
        # A refusal never enqueues, so no Confirm ever comes to consume the
        # hold the read phase just armed: close it now instead of pinning
        # ~640MB for HOLD_BROWSER_SECS for nothing. No pending lock is held
        # here (the enqueue decision below has not run), so taking the site
        # lock inside close_held cannot deadlock against the commit path.
        await session_mod.close_held(chat.id)
        return
    record: pending_mod.Pending = {
        "path": path,
        "steps": steps,
        "reported": reported,
        "scratch": scratch,
        "date": None,
    }
    # Decide and stage atomically: without the lock, two Screenshots
    # finishing their browser reads together would both see no live record
    # and both stage as active -- the exact confusion this queue removes.
    async with pending_mod.lock_for(chat.id):
        if pending_mod.has_live(chat.id):
            position = pending_mod.enqueue(chat.id, record)
            await prog.close(
                words.queued(reported, position), parse_mode=ParseMode.MARKDOWN
            )
            return
        await present_confirmation(chat.id, msg, record)
