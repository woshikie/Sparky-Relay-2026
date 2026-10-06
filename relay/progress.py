"""Progress reporting for the long part of a Screenshot.

The browser takes a while: launching Firefox, hydrating a React page, running
PaddleOCR in the page. A silent minute looks broken, which is why the old
fixed spinner animation was there -- except it finished after 1.4s, before any
of the real work had started, so it only ever animated over nothing.

This replaces it with a checklist that fills in as the work happens, and
deliberately keeps the reporting *out* of the path of the work:

  * `relay_site` runs in a worker thread. It cannot touch the event loop, and
    must not block on a network round-trip to Telegram either. So it calls
    `note()`, which appends to a plain list and returns.
  * One task in the loop polls that list and edits the message. A slow or
    throttled edit cannot hold up Firefox.

Edits are throttled and every failure is swallowed: Telegram rate-limits edits
to a message, and a submission must never fail because a progress line did.
"""
import asyncio
import time

# The order matters: it is the order the work happens in, so a reader can see
# how far along they are at a glance.
STEPS = (
    ("browser", "Starting a browser"),
    ("signin", "Signing you in"),
    ("site", "Opening the site"),
    ("upload", "Uploading your screenshot"),
    ("read", "Waiting for the site to read it"),
    ("closing", "Closing the browser"),
)

LABELS = dict(STEPS)
ORDER = [k for k, _ in STEPS]

# Telegram tolerates roughly one edit per second per message; well under that
# keeps it quiet. Anything faster just earns a 429 and then a RetryAfter.
MIN_INTERVAL = 1.2

DONE = "\u2705"
DOING = "\u23f3"
TODO = "\u2022"


class Progress:
    """A checklist that fills in as the Relay works.

    Constructed with the message to edit. `note()` is safe to call from a worker
    thread; `finish()` and `close()` must run on the loop.
    """

    def __init__(self, message=None, clock=time.monotonic, loop=None):
        self._msg = message
        self._clock = clock
        self._loop = loop or asyncio.get_event_loop()
        self._done = []            # keys, in the order they were reached
        self._current = None       # key being worked on now
        self._started = self._clock()
        self._last_edit = 0.0
        self._last_text = None
        self._task = None

    # --------------------------------------------------------- thread side

    def note(self, step):
        """Record that `step` has begun. Returns immediately.

        Called from the worker thread, so: no loop calls, no awaits, no network.
        list.append is atomic under the GIL, which is all the safety needed for
        one append.
        """
        if step in self._done or step == self._current:
            return
        if self._current is not None:
            self._done.append(self._current)
        self._current = step

    def finish(self, step=None):
        """Mark the current step complete, from the worker thread."""
        if step:
            self.note(step)
        if self._current is not None and self._current not in self._done:
            self._done.append(self._current)
        self._current = None

    # ----------------------------------------------------------- loop side

    def render(self):
        """The checklist, as plain text with no markup.

        No markup on purpose: this is edited many times a minute, and a
        Markdown error would kill the message at exactly the moment the user
        is waiting for it. Emoji only.
        """
        elapsed = max(0, int(self._clock() - self._started))
        lines = ["Working on it \u2014 %ds" % elapsed, ""]
        reached = list(self._done)
        if self._current:
            reached.append(self._current)
        for key in ORDER:
            label = LABELS[key]
            if key in reached and key != self._current:
                lines.append("%s %s" % (DONE, label))
            elif key == self._current:
                lines.append("%s %s\u2026" % (DOING, label))
            else:
                lines.append("%s %s" % (TODO, label))
        return "\n".join(lines)

    async def _render_loop(self):
        while True:
            await asyncio.sleep(MIN_INTERVAL)
            if self._clock() - self._last_edit < MIN_INTERVAL:
                continue
            await self._edit(self.render())

    async def _edit(self, text):
        """Best-effort edit. Returns whether it landed.

        Identical text is skipped here rather than in the poller, so `push()`
        cannot re-send a line the message already shows either.

        Every exception is swallowed on purpose. Telegram rate-limits edits per
        message; a 429 must cost a progress line, not the submission.
        """
        if self._msg is None:
            return False
        if text == self._last_text:
            return False
        try:
            await self._msg.edit_text(text)
        except Exception:
            # TelegramError, TimedOut, and anything the library raises for a
            # message we no longer own. Progress is decoration.
            return False
        self._last_edit = self._clock()
        self._last_text = text
        return True

    async def start(self):
        """Begin reporting. Called once the message exists."""
        await self._edit(self.render())
        self._task = asyncio.ensure_future(self._render_loop())
        return self

    async def push(self):
        """Render now, bypassing the pace but not the duplicate check.

        Step boundaries are the moments worth showing, so they should not wait
        out the interval -- but a step that renders the same line as the last
        one is not worth sending at all.
        """
        return await self._edit(self.render())

    async def stop(self):
        """Stop the reporter, leaving the message as it is.

        Needed on failure paths, which delete the message outright: a reporter
        still running would keep editing a message that no longer exists, one
        time per second, until something raised.
        """
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._task = None

    async def close(self, text=None, parse_mode=None):
        """Stop reporting and put the final text on the message.

        The last edit is the one that counts, so it is not throttled.
        """
        await self.stop()
        if text is None:
            return None
        if self._msg is None:
            return text
        try:
            if parse_mode is not None:
                await self._msg.edit_text(text, parse_mode=parse_mode)
            else:
                await self._msg.edit_text(text)
        except Exception:
            return None
        return text

    # ------------------------------------------------------------- helpers

    @property
    def reached(self):
        return list(self._done) + ([self._current] if self._current else [])

    def elapsed(self):
        return max(0, int(self._clock() - self._started))
