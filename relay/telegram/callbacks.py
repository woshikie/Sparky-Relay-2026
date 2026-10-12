"""The confirmation flow: dates, the overwrite guard, and Commit.

Pending confirmations live in telegram.pending, keyed by (chat_id,
message_id). The guard is one-directional by design: a downgrade is
always challenged, an upgrade never is. Access is asked through
access.check(), like every other handler.
"""

import asyncio
import datetime
from contextlib import suppress
from typing import cast

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import relay.clock as clock
import relay.store.ledger as ledger
import relay.telegram.access as access
import relay.telegram.codec as codec
import relay.telegram.failures as failures
import relay.telegram.pending as pending_mod
import relay.telegram.session as session_mod
import relay.telegram.words as words
from relay.site.parsing import parse_profile
from relay.telegram.datepicker import EVENT_START_MONTH, EVENT_START_YEAR
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
    if not access.check(chat.id, getattr(chat, "username", None)):
        await q.answer("not authorised", show_alert=True)
        return
    _, kind, rest = codec.decode(q.data)

    # Navigation taps (back/pick/prev/next/none, and anything unrecognised)
    # return before the refresh under the pending lock, so refresh here:
    # paging the calendar past the deadline must not lose the hold
    # mid-decision. Date kinds (today/yday/day) are deliberately excluded --
    # their refresh lives under the lock above the overwrite guard, and a
    # dedicated test pins that placement.
    if kind not in ("today", "yday", "day"):
        session_mod.refresh_hold(chat.id)

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
    today_now = clock.sg_today()
    if kind == "today":
        new_date = today_now
    elif kind == "yday":
        new_date = today_now - datetime.timedelta(days=1)
    elif kind == "back":
        await q.edit_message_reply_markup(reply_markup=kb_date_default())
        await q.answer()
        return
    elif kind == "pick":
        # Non-local safety: pick/prev/next funnel through kb_pick_date,
        # which funnels through keyboard()'s clamp, so no payload here can
        # page the grid outside [October 2026, max(today's month,
        # October 2026)].
        y, m = int(rest[0]), int(rest[1])
        await q.edit_message_text(
            "\U0001f4c5 Pick the activity date.", reply_markup=kb_pick_date(y, m)
        )
        await q.answer()
        return
    elif kind in ("prev", "next"):
        # Clamped as pick above: kb_pick_date clamps the target into the
        # window, so a crafted out-of-window payload still lands inside it.
        await q.edit_message_reply_markup(
            reply_markup=kb_pick_date(int(rest[0]), int(rest[1]))
        )
        await q.answer()
        return
    elif kind == "day":
        # Server-side guard (see below): the payload names the date, so it
        # is assigned here and judged with the rest after the branch.
        new_date = datetime.date.fromisoformat(rest[0])
    elif kind == "none":
        # Padding in the calendar grid. Answer, so the client stops spinning.
        await q.answer()
        return
    else:
        await q.answer()
        return

    if new_date is not None:
        # One shared guard for every kind that sets a date, judged against
        # the clock bound once above (no second read, so a tap straddling
        # SGT midnight cannot compute with one day and judge with the
        # next). today needs no *ceiling* guard — it is the ceiling — but
        # the floor applies to every kind: on 2026-10-01 yday is 2026-09-30,
        # and before the event today itself is pre-event.
        if new_date < datetime.date(EVENT_START_YEAR, EVENT_START_MONTH, 1):
            await q.answer(words.pre_event_day())
            return
        if new_date > today_now:
            await q.answer(words.future_day())
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
        # Date activity means the confirmation is still alive: keep its held
        # browser from lapsing mid-decision. Above the guard on purpose: a
        # challenged downgrade still sets a date, so both the warning and the
        # confirming paths extend the deadline. Navigation
        # (back/pick/prev/next/none) returned above and never reaches here.
        session_mod.refresh_hold(chat.id)
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
    if not access.check(chat.id, getattr(chat, "username", None)):
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
            scratch = st.get("scratch")
            await q.edit_message_text(words.cancelled())
            await q.answer()
            await _advance_chat(chat.id, msg)
            # Photo B reused/extended the same per-chat hold while queued
            # behind A, so cancelling A into B must not destroy what B
            # needs. Read under the lock so the close below cannot race
            # the advance it just performed. This stays alongside the token
            # below on purpose: the token only catches a hold armed *after*
            # this snapshot, while still_live guards the hold B already
            # armed before it, which the token cannot distinguish.
            still_live = pending_mod.latest_for_chat(chat.id) is not None
            expected = session_mod.hold_deadline(chat.id)
        # The scratch message outlived its Screenshot: remove it now, best
        # effort. Outside the pending lock like the close below -- the
        # SITE lock must not be taken while the pending lock is held, and
        # scratch.delete() takes neither lock, so it runs out here too.
        with suppress(TelegramError):
            if scratch is not None:
                await scratch.delete()
        # An explicit cancel ends the wait: close the held browser now
        # rather than letting the hold lapse -- but only when nothing is
        # still live (no queue advanced into the hold). Outside the pending
        # lock on purpose: close_held takes the site lock, while the commit
        # path takes the pending lock while holding the site lock
        # (site->pending), so closing in here (pending->site) would deadlock
        # a concurrent Confirm+Cancel on one chat. The token guards the
        # remaining race: a photo intake arming a fresh hold between this
        # lock's release and the site lock's acquire moves the deadline, so
        # the close no-ops instead of destroying the new hold.
        if not still_live:
            await session_mod.close_held(chat.id, expected_until=expected)
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

    # The read phase usually held the browser for this chat, so this reuses
    # it instead of relaunching; otherwise it re-opens, re-uploads the same
    # Screenshot, then sets the date and commits in one go. Either way the
    # commit closes it (hold=False): the hold only ever delays one close.
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
