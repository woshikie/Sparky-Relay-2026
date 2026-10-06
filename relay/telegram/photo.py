"""Screenshot intake: saving the photo, reading the Detected Steps.

The browser closes as soon as the number is read: holding ~640MB open while
the user decides on a date is not affordable on a 1GB host. Commit re-opens
it later and re-reads, and the two reads must agree.
"""
import asyncio
import io
import os

from PIL import Image
from telegram import Update
from telegram.constants import ParseMode

from relay import config
from relay import progress as progress_mod
from relay.site import driver as relay_site
import relay.telegram.access as access
import relay.telegram.words as words
from relay.telegram.callbacks import PENDING
from relay.telegram.keyboards import kb_date_default
from relay.telegram.prompts import ask_credentials
import relay.telegram.failures as failures
import relay.telegram.session as session_mod
from relay.telegram.session import log


# Longest edge of the Screenshot we will keep. The site downscales to 1200px
# anyway, and smaller photos upload faster on a 1GB VM.
MAX_EDGE = 1200


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
    session_mod.log(ctx, "photo saved %s (%dx%d)" % (os.path.basename(path), *im.size))
    return path


# ----------------------------------------------------------------------

async def on_photo(update: Update, ctx):
    chat = update.effective_chat
    msg = update.effective_message
    d = access.check(chat.id, getattr(chat, "username", None))
    if not d:
        await msg.reply_text(words.access_refused(d), parse_mode=ParseMode.MARKDOWN)
        return

    # One message, edited as the work happens. It used to be a fixed four-frame
    # animation that finished in 1.4s -- before the browser had even launched --
    # so the long part, which is all of it, happened in silence.
    scratch = await msg.reply_text(words.scanning(
        getattr(chat, "username", None) or "your screenshot"),
        parse_mode=ParseMode.MARKDOWN)
    prog = await progress_mod.Progress(scratch).start()

    path = await save_photo(update, ctx)
    if not path:
        await prog.close("That does not look like an image. Send a "
                         "screenshot of your tracker's day view.")
        return

    # Read the number. The browser closes as soon as we have it: on a 1GB host
    # holding ~640MB open while the user decides on a date is not affordable.
    try:
        async with session_mod.browser_session(prog) as r:
            await session_mod.sign_in(chat.id, prog)
            steps, reported = await asyncio.to_thread(r.upload, path)
    except Exception as e:
        await prog.stop()
        await scratch.delete()
        alert, alarm, log_line = await failures.explain(
            msg, e, operation="Upload",
            start_prompt=lambda: ask_credentials(msg, chat.id))
        if log_line:
            session_mod.log(ctx, log_line)
        return

    plausible = relay_site.MIN_STEPS <= steps <= relay_site.MAX_STEPS
    # The last edit is the one the user reads, so it is never throttled.
    await prog.close(words.ocr_read(steps, reported, plausible),
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
