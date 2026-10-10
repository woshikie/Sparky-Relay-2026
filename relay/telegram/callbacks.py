"""The confirmation flow: dates, the overwrite guard, and Commit.

Pending confirmations live in telegram.pending, keyed by (chat_id,
message_id). The guard is one-directional by design: a downgrade is
always challenged, an upgrade never is. authorised() lives here because the
callbacks are its only callers.
"""

import asyncio
import datetime
from contextlib import suppress
from typing import cast

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import relay.store.ledger as ledger
import relay.telegram.access as access
import relay.telegram.codec as codec
import relay.telegram.failures as failures
import relay.telegram.pending as pending_mod
import relay.telegram.session as session_mod
import relay.telegram.words as words
from relay.clock import sg_today
from relay.site.parsing import parse_profile
from relay.telegram.keyboards import (
    kb_confirm,
    kb_date_default,
    kb_overwrite,
    kb_pick_date,
)
from relay.telegram.prompts import ask_credentials, present_confirmation


async def cb_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    """Every date button routes here, so every one of them must be answered.

    A callback that raises leaves the button spinning in the client for good:
    Telegram gives no error and no reply, which is exactly what "it's stuck"
    looks like. So the whole body runs guarded, and an unexpected failure says
    so rather than hanging. cb_ok already worked this way; cb_date did not,
    and a malformed payload -- a stale grid from before a restart, say --
    reached int() unguarded.
    """
    q = update.callback_query
    assert q is not None
    try:
        await _cb_date(update, ctx)
    except Exception as e:
        session_mod.log(ctx, "date callback error: %r" % (e,))
        with suppress(TelegramError):
            await q.answer(
                "something went wrong — send the screenshot again", show_alert=True
            )


async def _cb_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    q = update.callback_query
    chat = update.effective_chat
    msg = update.effective_message
    assert q is not None and chat is not None and msg is not None
    if not authorised(ctx, chat.id):
        await q.answer("not authorised", show_alert=True)
        return
    _, kind, rest = codec.decode(q.data)

    key: tuple[int, int] | None = None
    st: pending_mod.Pending | None = None
    if msg.reply_to_message is not None:
        key = (chat.id, msg.reply_to_message.message_id)
        st = pending_mod.get(key)
    if st is None:
        # attach to the most recent pending for this chat
        found = pending_mod.latest_for_chat(chat.id)
        if found is None:
            async with pending_mod.lock_for(chat.id):
                record = pending_mod.take_next(chat.id)
                if record is None:
                    await q.answer(
                        "This request expired — send the screenshot again.",
                        show_alert=True,
                    )
                    await q.edit_message_text("Expired. Send the screenshot again.")
                    return
                await present_confirmation(chat.id, msg, record)
                await q.answer()
            return
        key, st = found

    new_date: datetime.date | None = None
    if kind == "today":
        new_date = sg_today()
    elif kind == "yday":
        new_date = sg_today() - datetime.timedelta(days=1)
    elif kind == "back":
        await q.edit_message_reply_markup(reply_markup=kb_date_default())
        await q.answer()
        return
    elif kind == "pick":
        y, m = int(rest[0]), int(rest[1])
        await q.edit_message_text(
            "\U0001f4c5 Pick the activity date.", reply_markup=kb_pick_date(y, m)
        )
        await q.answer()
        return
    elif kind == "prev":
        # The target month arrives already resolved: datepicker.shift() owns the
        # wrap, so there is no January/December arithmetic here to get wrong.
        await q.edit_message_reply_markup(
            reply_markup=kb_pick_date(int(rest[0]), int(rest[1]))
        )
        await q.answer()
        return
    elif kind == "next":
        await q.edit_message_reply_markup(
            reply_markup=kb_pick_date(int(rest[0]), int(rest[1]))
        )
        await q.answer()
        return
    elif kind == "day":
        new_date = datetime.date.fromisoformat(rest[0])
    elif kind == "future":
        # A day that has not happened yet. Answered, not silent, and the
        # pending record is untouched: no date is set.
        await q.answer(words.future_day())
        return
    elif kind == "none":
        # Padding in the calendar grid. Answer, so the client stops spinning.
        await q.answer()
        return
    else:
        await q.answer()
        return

    assert key is not None
    async with pending_mod.lock_for(chat.id):
        st = pending_mod.get(key)
        if st is None:
            await q.answer(
                "This request expired — send the screenshot again.",
                show_alert=True,
            )
            await q.edit_message_text("Expired. Send the screenshot again.")
            return
        # Every branch above either returned or assigned a date; the assert
        # pins that, since the intake starts the record at None.
        assert new_date is not None, "date button without a date"
        st["date"] = new_date
        date = new_date
        iso = date.isoformat()
        label = "%s %s, %d" % (date.strftime("%B"), ordinal(date.day), date.year)
        st["iso"] = iso
        st["label"] = label

        # The number being confirmed comes from the pending record, not from
        # the callback data: the callback only carries a date. Reading it
        # from the record is also what makes the Confirmation impossible to
        # drift from the number the user approved.
        steps = st.get("steps")
        reported = st.get("reported")

        # Overwrite guard: only the downward direction is challenged.
        # The site is the authority, not our record of what we wrote: a day
        # the user entered by hand is invisible until a sync, and the guard
        # has to know about it or it will overwrite a day the bot never
        # touched.
        prev = ledger.current_value(iso)
        st["prev"] = prev
        # current_value rows are dict[str, object]; the guard compares ints,
        # and the site only ever stores ints, so this says so once instead
        # of at every use below.
        prev_steps = cast(int, prev["steps"]) if prev is not None else None
        if prev_steps is not None and steps < prev_steps:
            await q.edit_message_text(
                words.overwrite_warning(steps, prev_steps, label),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_overwrite(prev_steps),
            )
            await q.answer("this would lower your recorded steps", show_alert=True)
            return
        if prev_steps is not None and steps > prev_steps:
            await q.edit_message_text(
                words.overwrite_upgrade(steps, prev_steps, label),
                parse_mode=ParseMode.MARKDOWN,
            )
            await q.answer()

        await q.edit_message_text(
            words.confirming(steps, reported, label, iso),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm(reported),
        )
        await q.answer()


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


async def _advance_chat(chat_id: int, msg: Message) -> None:
    """Present the next queued Screenshot, if the chat has no live one.

    Call with lock_for(chat_id) held: pop-advance-present must be atomic
    against photo intake deciding on the same chat.
    """
    record = pending_mod.take_next(chat_id)
    if record is None:
        return
    await present_confirmation(chat_id, msg, record)


async def cb_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE | None) -> None:
    q = update.callback_query
    chat = update.effective_chat
    msg = update.effective_message
    assert q is not None and chat is not None and msg is not None
    if not authorised(ctx, chat.id):
        await q.answer("not authorised", show_alert=True)
        return
    try:
        _, action, _ = codec.decode(q.data)
    except ValueError:
        await q.answer(
            "something went wrong \u2014 send the screenshot again", show_alert=True
        )
        return

    found = pending_mod.latest_for_chat(chat.id)
    if found is None:
        async with pending_mod.lock_for(chat.id):
            record = pending_mod.take_next(chat.id)
            if record is None:
                await q.answer(
                    "This request expired — send the screenshot again.",
                    show_alert=True,
                )
                await q.edit_message_text("Expired. Send the screenshot again.")
                return
            try:
                await present_confirmation(chat.id, msg, record)
            except Exception:
                # Broad on purpose: a tap must always be answered, and the
                # record is already requeued, so nothing is lost by catching.
                with suppress(Exception):
                    await q.answer(
                        "something went wrong \u2014 send the screenshot again",
                        show_alert=True,
                    )
                return
            await q.answer()
        return
    key: tuple[int, int]
    st: pending_mod.Pending | None
    key, st = found
    assert st is not None

    if action == "cancel":
        async with pending_mod.lock_for(chat.id):
            st = pending_mod.pop(key, None)
            if st is None:
                await q.answer(
                    "This request expired — send the screenshot again.",
                    show_alert=True,
                )
                await q.edit_message_text("Expired. Send the screenshot again.")
                return
            await q.edit_message_text(words.cancelled())
            await q.answer()
            await _advance_chat(chat.id, msg)
        return

    date = st.get("date")
    if not date:
        await q.answer("Pick a date first.", show_alert=True)
        return

    label = st["label"]
    with suppress(TelegramError):
        await q.edit_message_text(
            "⏳ Recording %s steps for %s…" % (st["reported"], label)
        )

    # The browser was closed after reading the number, so re-open it, re-upload
    # the same Screenshot, then set the date and Commit in one go. This is a
    # deliberate trade: a second OCR pass costs ~25s of the host's RAM twice
    # instead of holding it once for as long as the user takes to decide.
    site_text = ""
    try:
        async with session_mod.browser_session(chat.id) as r:
            # Re-reading must agree with what the user confirmed, or the
            # Screenshot is not the one they agreed to submit.
            steps2, reported2 = await asyncio.to_thread(r.upload, st["path"])
            async with pending_mod.lock_for(chat.id):
                live = pending_mod.get(key)
                if live is None:
                    await q.answer(
                        "This request expired — send the screenshot again.",
                        show_alert=True,
                    )
                    await q.edit_message_text("Expired. Send the screenshot again.")
                    return
                if steps2 != live["steps"]:
                    st = pending_mod.pop(key, None)
                    if st is None:
                        await q.answer(
                            "This request expired — send the screenshot again.",
                            show_alert=True,
                        )
                        await q.edit_message_text("Expired. Send the screenshot again.")
                        return
                    await msg.reply_text(
                        words.ocr_changed(st["reported"], reported2, label),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await q.answer("the site's read changed", show_alert=True)
                    await _advance_chat(chat.id, msg)
                    return
            await asyncio.to_thread(r.set_date, date)
            site_text = await asyncio.to_thread(r.commit, st["steps"])
    except Exception as e:
        async with pending_mod.lock_for(chat.id):
            st = pending_mod.pop(key, None)
            if st is None:
                await q.answer(
                    "This request expired — send the screenshot again.",
                    show_alert=True,
                )
                await q.edit_message_text("Expired. Send the screenshot again.")
                return
            alert, alarm, log_line = await failures.explain(
                msg,
                e,
                operation="Commit",
                start_prompt=lambda: ask_credentials(msg, chat.id),
            )
            if log_line:
                session_mod.log(ctx, log_line)
            await q.answer(alert, show_alert=alarm)
            await _advance_chat(chat.id, msg)
        return

    async with pending_mod.lock_for(chat.id):
        st = pending_mod.pop(key, None)
        if st is None:
            await q.answer(
                "This request expired — send the screenshot again.",
                show_alert=True,
            )
            await q.edit_message_text("Expired. Send the screenshot again.")
            return
        ledger.record(
            st["iso"],
            st["steps"],
            reported=st["reported"],
            site_label=st["label"],
            msg_id=msg.message_id,
        )

        with suppress(TelegramError):
            await msg.reply_text(
                words.recorded(st["reported"], st["label"], st["iso"], site_text),
                parse_mode=ParseMode.MARKDOWN,
            )
        # The record is already requeued on a failed present, so a failed
        # advance still records: the next tap promotes.
        with suppress(Exception):
            await _advance_chat(chat.id, msg)

    # House standing: a second browser launch, best effort, clearly separate
    # from the result. Skipped if memory is short — it is decoration.
    try:
        async with session_mod.browser_session() as r:
            board = await asyncio.to_thread(r.leaderboard_line)
        if board:
            profile = parse_profile(board)
            if profile:
                line = words.rank_line(
                    profile["total_steps"], profile["total_points"], profile["house"]
                )
                if line:
                    await msg.reply_text(line, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

    with suppress(TelegramError):
        await q.answer("recorded")


def authorised(ctx: ContextTypes.DEFAULT_TYPE | None, chat_id: int) -> bool:
    """Deprecated shim. Access now lives in access.check().

    Kept so nothing calls a removed function, but every handler should ask
    access.check() directly — it returns a reason, which the user needs to be
    told.
    """
    return bool(access.check(chat_id))
