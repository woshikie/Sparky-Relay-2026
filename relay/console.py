"""Timestamps on stdout.

`podman logs` can add a timestamp to each line, but only if you remember to ask
for it, and it does nothing for a plain `> file` redirect or `docker logs`
without the flag. More to the point, the thing you actually want to read is not
the wall clock -- it is "how long did that take". Launching the browser is the
step that goes slow on a 1GB host, and without an elapsed counter every line
looks equally instantaneous.

So stdout is wrapped once, at startup, rather than adding a `now()` call to
fifteen print sites. That also catches anything the libraries print, which is
how the geckodriver output ends up interleaved with ours.

    19:04:31  +0.0s  [relay] starting
    19:04:31  +0.2s  [relay] RAM budget: 909MB usable
    19:04:44  +13.1s  [relay] browser started

The wrapper buffers partial lines, because a single print() is not guaranteed
to arrive in one write() and prefixing half a line would be worse than useless.
"""

import sys
import time
from collections.abc import Callable
from typing import TextIO

# Both, because they answer different questions: the wall clock lines up
# against what you were doing, the offset shows how long a step took.
CLOCK_FORMAT = "%H:%M:%S"


def stamp(now: float, since_start: float) -> str:
    """One log prefix: '19:04:31  +13.1s'."""
    wall = time.strftime(CLOCK_FORMAT, time.localtime(now))
    return "%s  +%.1fs" % (wall, now - since_start)


class TimestampedStream:
    """A write-through wrapper that prefixes each complete line.

    Implements write/flush and nothing else, which is all print() needs.
    Anything that assumes a real file (isatty, fileno) is answered so a library
    doing colour detection does not explode.
    """

    def __init__(
        self,
        inner: TextIO,
        clock: Callable[[], float] = time.time,
        prefix: str | None = None,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._start = clock()
        self._prefix = prefix  # for tests; defaults to stamp()
        self._partial = ""

    def _prefix_for(self) -> str:
        if self._prefix is not None:
            return self._prefix
        return stamp(self._clock(), self._start)

    def write(self, data: str) -> int | None:
        # data is typed str, but something writing bytes to a text stream
        # arrives here too -- hence the isinstance, not wishful thinking.
        if not isinstance(data, str):
            # A non-str write means something is writing bytes to a text
            # stream. Pass it through untouched rather than mangling it.
            self._flush_partial()
            return self._inner.write(data)
        self._partial += data
        if "\n" not in self._partial:
            return None
        *lines, self._partial = self._partial.split("\n")
        out = []
        for line in lines:
            # An empty line stays empty: geckodriver and friends emit blanks,
            # and prefixing them adds noise rather than information.
            out.append(line if line == "" else "%s  %s" % (self._prefix_for(), line))
        return self._inner.write("\n".join(out) + "\n")

    def _flush_partial(self) -> None:
        if self._partial:
            text, self._partial = self._partial, ""
            self._inner.write("%s  %s\n" % (self._prefix_for(), text))

    def flush(self) -> None:
        self._flush_partial()
        return self._inner.flush()

    # ---- things libraries ask a stream about ----------------------------

    def isatty(self) -> bool:
        return getattr(self._inner, "isatty", lambda: False)()

    def fileno(self) -> int:
        return self._inner.fileno()

    @property
    def encoding(self) -> str:
        # Some sinks (io.StringIO) have no encoding at all and report None,
        # which makes libraries that consult it fall over.
        return getattr(self._inner, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str | None:
        return getattr(self._inner, "errors", None)


def install(
    stream: TextIO | None = None, clock: Callable[[], float] = time.time
) -> TimestampedStream:
    """Wrap stdout, once. Returns the wrapper, or the stream if already done.

    Idempotent, because main() is not the only thing that might run: the
    verify script in the image calls install() too.
    """
    target = stream if stream is not None else sys.stdout
    if isinstance(target, TimestampedStream):
        return target
    wrapped = TimestampedStream(target, clock=clock)
    sys.stdout = wrapped
    return wrapped


def uninstall() -> TextIO:
    """Put the original stdout back. For tests, and for a clean handoff."""
    out = sys.stdout
    if isinstance(out, TimestampedStream):
        sys.stdout = out._inner
        return out._inner
    return out
