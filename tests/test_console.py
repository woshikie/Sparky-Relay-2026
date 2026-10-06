"""Log timestamps.

Wrapping stdout once beats adding a now() call to every print site: it catches
the geckodriver output interleaved with ours, and it works whether the logs are
read with `podman logs`, `docker logs`, or a plain redirect.

The offset matters more than the wall clock. Launching the browser is the step
that goes slow on a 1GB host; with no elapsed counter every line reads as
instantaneous and there is nothing to compare a bad run against.
"""
import io
import time

import pytest

from relay import console


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def buf():
    return io.StringIO()


def lines(buf):
    """Non-empty output lines."""
    return [l for l in buf.getvalue().split("\n") if l]


def all_lines(buf):
    """Every line, blanks kept -- for testing that blanks survive."""
    return buf.getvalue().split("\n")[:-1]


class MixedSink:
    """Accepts both str and bytes, like a real stdout replacement might see."""

    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(data)

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(p if isinstance(p, str) else p.decode()
                        for p in self.parts)


# ------------------------------------------------------------- the prefix

def test_a_line_gets_a_wall_clock_time(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("[relay] starting\n")
    assert time.strftime("%H:%M:%S", time.localtime(1_700_000_000.0)) \
        in lines(buf)[0]


def test_a_line_gets_an_elapsed_offset(buf):
    """The number you actually compare a slow run against."""
    clock = Clock()
    c = console.TimestampedStream(buf, clock=clock)
    clock.advance(13.1)
    c.write("[relay] browser started\n")
    assert "+13.1s" in lines(buf)[0]


def test_the_first_line_is_plus_zero(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("hello\n")
    assert "+0.0s" in lines(buf)[0]


def test_the_original_message_survives(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("[relay] signed in\n")
    assert lines(buf)[0].endswith("[relay] signed in")


# ------------------------------------------------------------ partial lines

def test_a_partial_write_is_not_prefixed_yet(buf):
    """Prefixing half a line would be worse than useless."""
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("[relay] bro")
    assert buf.getvalue() == ""


def test_a_partial_write_is_completed_on_the_next_one(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("[relay] bro")
    c.write("wser started\n")
    assert lines(buf) == ["%s  [relay] browser started"
                          % c._prefix_for()]


def test_a_byte_at_a_time_still_produces_one_prefix(buf):
    """print() does not promise a single write()."""
    c = console.TimestampedStream(buf, clock=Clock())
    for ch in "abc\n":
        c.write(ch)
    assert len(lines(buf)) == 1
    assert lines(buf)[0].endswith("abc")


def test_a_whole_buffer_of_text_is_prefixed_once_per_line(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("one\ntwo\nthree\n")
    assert len(lines(buf)) == 3


def test_several_lines_in_one_write_all_get_a_prefix(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("a\nb\n")
    for l in lines(buf):
        assert "+" in l and "s" in l


def test_flush_pushes_out_the_partial_line(buf):
    """A last line without a newline must not be lost on shutdown."""
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("tail with no newline")
    assert buf.getvalue() == ""
    c.flush()
    assert lines(buf)[0].endswith("tail with no newline")


def test_flush_on_an_empty_buffer_adds_nothing(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    c.flush()
    assert buf.getvalue() == ""


def test_print_works_through_the_wrapper(buf):
    c = console.TimestampedStream(buf, clock=Clock())
    print("[relay] via print", file=c, flush=True)
    assert lines(buf)[0].endswith("[relay] via print")


# ----------------------------------------------------------------- noise

def test_a_blank_line_stays_blank(buf):
    """geckodriver emits blanks; prefixing them is noise, not information."""
    c = console.TimestampedStream(buf, clock=Clock())
    c.write("a\n\nb\n")
    prefix = c._prefix_for()
    assert all_lines(buf) == ["%s  a" % prefix, "", "%s  b" % prefix]


def test_a_non_string_write_passes_through():
    """Better untouched than mangled."""
    sink = MixedSink()
    c = console.TimestampedStream(sink, clock=Clock())
    c.write(b"raw bytes\n")
    assert sink.getvalue() == "raw bytes\n"


def test_a_non_string_write_does_not_lose_a_partial_line():
    sink = MixedSink()
    c = console.TimestampedStream(sink, clock=Clock())
    c.write("pending")
    c.write(b"raw\n")
    assert "pending" in sink.getvalue()


# --------------------------------------------------------------- passthru

def test_isatty_is_answered(buf):
    """A library doing colour detection must not explode on the wrapper."""
    assert console.TimestampedStream(buf).isatty() in (True, False)


def test_encoding_is_reported():
    """io.StringIO has none and says so with None; utf-8 is the safe answer."""
    assert console.TimestampedStream(io.StringIO()).encoding == "utf-8"


def test_flush_reaches_the_inner_stream():
    class Counting(io.StringIO):
        flushes = 0

        def flush(self):
            Counting.flushes += 1
            super().flush()

    inner = Counting()
    c = console.TimestampedStream(inner, clock=Clock())
    c.flush()
    assert Counting.flushes == 1


# ---------------------------------------------------------------- install

def test_install_wraps_stdout():
    import sys
    original = sys.stdout
    try:
        wrapped = console.install()
        assert sys.stdout is wrapped
    finally:
        console.uninstall()
    assert sys.stdout is original


def test_install_is_idempotent():
    """main() is not the only entry point; the image's verify script runs too."""
    import sys
    try:
        first = console.install()
        second = console.install()
        assert first is second
    finally:
        console.uninstall()


def test_uninstall_is_safe_when_not_installed():
    import sys
    original = sys.stdout
    console.uninstall()
    assert sys.stdout is original


def test_stamp_shows_the_wall_clock():
    out = console.stamp(1_700_000_000.0, 1_700_000_000.0)
    assert out.startswith(time.strftime("%H:%M:%S",
                                        time.localtime(1_700_000_000.0)))
    assert "+0.0s" in out


# --------------------------------------------------- the remaining branches

def test_a_caller_supplied_prefix_is_used(buf):
    """For tests, and for anything that wants its own format."""
    c = console.TimestampedStream(buf, clock=Clock(), prefix="T")
    c.write("hello\n")
    assert lines(buf)[0].startswith("T  ")


def test_fileno_is_delegated():
    """A library that asks for the descriptor must get a real answer."""
    class WithFileno(io.StringIO):
        def fileno(self):
            return 7
    assert console.TimestampedStream(WithFileno()).fileno() == 7


def test_errors_is_reported_when_the_stream_has_one():
    class WithErrors(io.StringIO):
        errors = "replace"
    assert console.TimestampedStream(WithErrors()).errors == "replace"


def test_errors_is_none_when_the_stream_has_none():
    assert console.TimestampedStream(io.StringIO()).errors is None
