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

# Both, because they answer different questions: the wall clock lines up
# against what you were doing, the offset shows how long a step took.
CLOCK_FORMAT = "%H:%M:%S"


def stamp(now, since_start):
    """One log prefix: '19:04:31  +13.1s'."""
    wall = time.strftime(CLOCK_FORMAT, time.localtime(now))
    return "%s  +%.1fs" % (wall, now - since_start)


class TimestampedStream:
    """A write-through wrapper that prefixes each complete line.

    Implements write/flush and nothing else, which is all print() needs.
    Anything that assumes a real file (isatty, fileno) is answered so a library
    doing colour detection does not explode.
    """

    def __init__(self, inner, clock=time.time, prefix=None):
        self._inner = inner
        self._clock = clock
        self._start = clock()
        self._prefix = prefix          # for tests; defaults to stamp()
        self._partial = ""

    def _prefix_for(self):
        if self._prefix is not None:
            return self._prefix
        return stamp(self._clock(), self._start)

    def write(self, data):
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
            out.append(line if line == "" else "%s  %s" % (self._prefix_for(),
                                                           line))
        return self._inner.write("\n".join(out) + "\n")

    def _flush_partial(self):
        if self._partial:
            text, self._partial = self._partial, ""
            self._inner.write("%s  %s\n" % (self._prefix_for(), text))

    def flush(self):
        self._flush_partial()
        return self._inner.flush()

    # ---- things libraries ask a stream about ----------------------------

    def isatty(self):
        return getattr(self._inner, "isatty", lambda: False)()

    def fileno(self):
        return self._inner.fileno()

    @property
    def encoding(self):
        # Some sinks (io.StringIO) have no encoding at all and report None,
        # which makes libraries that consult it fall over.
        return getattr(self._inner, "encoding", None) or "utf-8"

    @property
    def errors(self):
        return getattr(self._inner, "errors", None)


def install(stream=None, clock=time.time):
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


def uninstall():
    """Put the original stdout back. For tests, and for a clean handoff."""
    if isinstance(sys.stdout, TimestampedStream):
        sys.stdout = sys.stdout._inner
    return sys.stdout
