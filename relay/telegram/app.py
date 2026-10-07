"""Application wiring: building the bot and registering handlers.

Kept apart from the handlers so importing a handler never constructs an
Application, and importing this module never starts polling.
"""

from datetime import time as wallclock

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import relay.store.db as db
import relay.store.policy as policy
import relay.telegram.access as access
import relay.telegram.session as session_mod
from relay import config, console, memory
from relay.store.backup import backup_job
from relay.telegram.callbacks import cb_date, cb_ok
from relay.telegram.commands import (
    on_log,
    on_start,
    on_status,
    on_sync,
    on_text,
)
from relay.telegram.photo import on_photo
from relay.telegram.prompts import on_credential_choice, on_login, on_logout

# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def main() -> None:
    # First thing, so a refusal is timestamped too -- a bot that dies on a
    # config error is exactly when you want the timestamp.
    #
    # require() raises SystemExit carrying the message, which Python prints to
    # stderr and exits 1. That is already "a plain message and a non-zero
    # exit, not a traceback", so there is nothing to catch here. It used to
    # catch errors.ConfigRefused, which require() never raises -- ConfigRefused
    # is the import-time refusal for an invalid ACCESS_MODE, a different error
    # on a different path.
    console.install()
    config.require()
    db.init()
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_ready)
        .build()
    )

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_start))
    app.add_handler(CommandHandler("login", on_login))
    app.add_handler(CommandHandler("logout", on_logout))
    app.add_handler(CommandHandler("log", on_log))
    app.add_handler(CommandHandler("sync", on_sync))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CallbackQueryHandler(cb_date, pattern=r"^dt:"))
    app.add_handler(CallbackQueryHandler(cb_ok, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_credential_choice, pattern=r"^cred:"))

    # Photos first, then text. A photo is never an answer to the Credentials
    # Prompt, and text has to reach on_text even though it is the catch-all.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.MimeType("image/"), on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue is None:
        raise SystemExit(
            "job-queue support is missing.\n"
            "  install it:  pip install 'python-telegram-bot[job-queue]'\n"
            "  the nightly ledger backup is scheduled through the job queue."
        )
    hh, mm = [int(x) for x in config.BACKUP_TIME.split(":")]
    # wallclock, not the time module: `time(hour=..., minute=...)` is a class.
    app.job_queue.run_daily(
        backup_job, wallclock(hour=hh, minute=mm), name="ledger-backup"
    )

    loaded, total = policy.init_secrets_from_env(config.SHARED_SECRETS)
    if loaded:
        print(
            "[relay] loaded %d Shared Secret(s) from the environment" % total,
            flush=True,
        )
    print("[relay] %s" % access.describe())
    if config.ACCESS_MODE == "blacklist":
        print(
            "[relay] WARNING: ACCESS_MODE=blacklist — any chat that finds "
            "this bot may drive it. See docs/adr/0006.",
            flush=True,
        )
    if config.has_preset_credentials():
        print(
            "[relay] preset credentials configured for %s; the bot will "
            "offer them at the Credentials Prompt" % config.SITE_USERNAME,
            flush=True,
        )
    else:
        print(
            "[relay] no preset credentials — you will be asked for a "
            "username and password",
            flush=True,
        )
    print("[relay] %s" % memory.describe())
    if not memory.budget()["can_launch"]:
        print(
            "[relay] WARNING: not enough free memory to run the browser. "
            "Screenshots will be refused until memory frees up. "
            "Consider a larger host — see docs/adr/0004."
        )
    print("[relay] starting")
    try:
        # Do NOT drop pending updates. A restart on a small host is routine, and
        # dropping means a "/start" sent during the restart vanishes with no
        # reply and no error -- which looks exactly like the bot being broken.
        # A stale Confirm button from before the restart is refused as expired,
        # which is right anyway: the pending request did not survive.
        app.run_polling(drop_pending_updates=False, close_loop=False)
    finally:
        session_mod.get_relay().stop()


async def on_ready(app: Application) -> None:
    print("[relay] online as @%s" % (await app.bot.get_me()).username, flush=True)
