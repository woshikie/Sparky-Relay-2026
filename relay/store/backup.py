"""Nightly ledger backup, delivered to Telegram itself.

Lives next to the ledger it dumps, not next to the text router: the only
thing it shared with commands.py was a file. Scheduled from telegram/app.py;
the re-export in relay.telegram keeps bot.backup_job working for the tests.
"""

import io
from typing import cast

from telegram.ext import ContextTypes

import relay.store.db as db
import relay.store.ledger as ledger
import relay.store.policy as policy
import relay.telegram.words as words
from relay.clock import sg_now


async def backup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the whole sqlite file to the oldest-known user."""
    chat_ids = [cast(int, u["chat_id"]) for u in policy.known_users()]
    if not chat_ids:
        return
    db.init()
    with open(db.DB, "rb") as f:
        data = f.read()
    stamp = sg_now().strftime("%Y-%m-%d")
    fname = "relay-ledger-%s.sqlite3" % stamp
    await ctx.bot.send_document(
        chat_id=chat_ids[0],
        document=io.BytesIO(data),
        filename=fname,
        caption="%s Ledger backup — %d Submission(s)."
        % (words.EMOJI["backup"], len(ledger.all_submissions())),
    )
