"""Nightly ledger backup, delivered to Telegram itself.

Lives next to the ledger it dumps, not next to the text router: the only
thing it shared with commands.py was a file. Scheduled from telegram/app.py;
the re-export in relay.telegram keeps bot.backup_job working for the tests.
"""

import io
from typing import Protocol, cast

import relay.store.db as db
import relay.store.ledger as ledger
import relay.store.policy as policy
from relay.clock import sg_now


class _DocumentBot(Protocol):
    """The slice of the Telegram Bot this job needs.

    Structural, so this module never imports telegram: dependency flows
    one way, telegram -> store, and the caption emoji below is inlined
    rather than borrowed from telegram.words for the same reason.
    """

    async def send_document(
        self,
        chat_id: int,
        document: io.BytesIO,
        filename: str,
        caption: str,
    ) -> object: ...


class _JobContext(Protocol):
    bot: _DocumentBot


async def backup_job(ctx: object) -> None:
    """Send the whole sqlite file to the oldest-known user.

    `ctx` is the scheduler's context, typed as object so this module never
    imports telegram: the cast below recovers the slice it needs.
    """
    job = cast(_JobContext, ctx)
    chat_ids = [cast(int, u["chat_id"]) for u in policy.known_users()]
    if not chat_ids:
        return
    db.init()
    with open(db.DB, "rb") as f:
        data = f.read()
    stamp = sg_now().strftime("%Y-%m-%d")
    fname = "relay-ledger-%s.sqlite3" % stamp
    await job.bot.send_document(
        chat_id=chat_ids[0],
        document=io.BytesIO(data),
        filename=fname,
        caption="\U0001f4be Ledger backup — %d Submission(s)."
        % (len(ledger.all_submissions())),
    )
