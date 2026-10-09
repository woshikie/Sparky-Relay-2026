"""Every string the bot sends must be valid Telegram Markdown.

This is a bug class, not a style check. Telegram validates entities server-side
and rejects the whole message if they do not parse, so a stray `**` means the
command silently does nothing — the user sees no reply at all and no error
locally. That is exactly how `/status` vanished: `access.describe()` returned
its own `**whitelist_claim**`, which was then nested inside another `**` pair.

Telegram's Markdown dialect (legacy "Markdown", parse_mode=Markdown) supports
bold with `*`, italic with `_`, and fixed-width with backticks. These checks
model that closely enough to catch unbalanced markers, which is the failure
that matters.
"""
import datetime
import re

import pytest

from relay import config, memory
from relay.telegram import access, words


def all_strings():
    """Every user-facing string, with representative arguments."""
    cases = []
    for name in dir(words):
        if name.startswith("_"):
            continue
        fn = getattr(words, name)
        if not callable(fn):
            continue
        for args in ((), ("x",), ("x", "y"), (1,), (1, 2, "x"),
                     (100, 200, "October 4th, 2026")):
            try:
                cases.append((name + repr(args), str(fn(*args))))
            except Exception:
                continue
    return cases


CASES = all_strings()


def callables_in_words():
    return [n for n in dir(words)
            if not n.startswith("_") and callable(getattr(words, n))]


def test_the_sweep_reaches_every_string():
    """Self-maintaining: adding a copy function must add it to the sweep."""
    assert len(CASES) >= len(callables_in_words())


def test_the_shape_sweep_reaches_every_string():
    assert len(SHAPES) == len(callables_in_words())


# Argument values by name and type, so each string is called with something its
# body can actually use. Filling every parameter with the same string is what
# made the first sweep useless: log_lines() indexes into what it is given, and
# rank_line() formats its totals with a thousands separator, so a string raised
# for reasons that had nothing to do with formatting.
NUMERIC = ("steps", "total_steps", "total_points", "count", "n", "new_steps",
           "old_steps", "wait", "retry_after", "written", "position")
SUBMISSIONS = [{"activity_date": "2026-10-04", "steps": 6532,
                "reported": "6,532", "recorded_at": "2026-10-05T21:00:00"}]

# What read_days() hands back: (date, steps) pairs, newest first.
SYNC_DAYS = [(datetime.date(2026, 10, 5), 4272),
             (datetime.date(2026, 10, 4), 6532)]


def _value_for(param):
    name = param.name
    if name == "subs":
        return SUBMISSIONS
    if name == "days":
        return SYNC_DAYS
    if name in NUMERIC:
        return 1000
    if name in ("decision",):
        return None
    if name == "name":
        return "Wai Gie"
    ann = str(param.annotation)
    for key, val in (("int", 1), ("float", 1.0), ("bool", True),
                     ("list", []), ("dict", {}), ("tuple", ())):
        if key in ann:
            return val
    return "x"


def calling_shapes():
    """Each string called with arguments matching its own signature.

    The earlier sweep passed fixed tuples and swallowed TypeError, which hid
    exactly the bug it was meant to find: `login_failed` and friends had a `%`
    bound to the last fragment of an implicit concatenation, so they raised
    unless called with an accidental arity.
    """
    import inspect
    cases = []
    for name in dir(words):
        if name.startswith("_"):
            continue
        fn = getattr(words, name)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        params = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                  and p.default is p.empty]
        args = [_value_for(p) for p in params]
        try:
            cases.append((name, str(fn(*args))))
        except Exception as e:      # a raise here is itself the finding
            cases.append((name, "RAISED %s: %s" % (type(e).__name__, e)))
    return cases


SHAPES = calling_shapes()


@pytest.mark.parametrize("label,text", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_string_formats_at_its_own_arity(label, text):
    """The implicit-concatenation trap.

        "a %s b"
        "c %s" % one_value

    binds the `%` to `"c %s"` alone, so the first placeholder raises
    `not all arguments converted`. It shipped in `welcome()`, which is how
    `/help` greeted you with a raw dict.
    """
    assert not text.startswith("RAISED"), "%s: %s" % (label, text[:120])


@pytest.mark.parametrize("label,text", CASES, ids=[c[0] for c in CASES])
def test_markers_are_balanced(label, text):
    r"""Escape sequences do not count: `\_` is a literal underscore.

    A CommonMark parser would not catch an unescaped `_` here, because
    CommonMark leaves intra-word underscores alone and Telegram does not. So
    this check counts markers outside escapes, which is what Telegram sees.
    """
    stripped = re.sub(r"\\.", "", text)      # drop escaped pairs
    for marker in ("*", "`"):
        assert stripped.count(marker) % 2 == 0, \
            "%s: odd number of %r" % (label, marker)


def test_md_escapes_what_telegram_treats_as_markup():
    """The library, not a hand-rolled version, and specifically `_`.

    `_` is what actually broke /status: `whitelist_claim` opened an italic
    that never closed, Telegram rejected the whole message, and there was no
    error anywhere to point at the cause.
    """
    out = words.md("whitelist_claim")
    assert words.md("whitelist_claim") == out
    # No bare underscore survives into the output.
    assert "\\" in out
    assert out.replace("\\", "") .count("_") == 1    # the underscore, escaped


def test_md_is_not_a_hand_rolled_approximation():
    """Pin it to the library, so a future edit cannot quietly diverge."""
    from telegram.helpers import escape_markdown
    for sample in ("whitelist_claim", "a*b_c[d]`e", "path/with_underscore"):
        assert words.md(sample) == escape_markdown(sample)


def test_code_neutralises_markup_entirely():
    """A code span is literal, which is why identifiers use it."""
    assert words.code("whitelist_claim") == "`whitelist_claim`"
    assert words.code("a`b") == "`ab`", "a backtick would close the span"


def test_a_value_cannot_break_out_of_a_code_span():
    assert words.code("` rm -rf /").count("`") == 2


def test_no_string_nests_bold():
    """`**a: **b** c**` is what broke /status: Telegram rejects the whole
    message, so the command silently did nothing.

    Walks the string as a tokenizer -- alternating open and close -- so
    `these **2** steps` reads as one sibling span rather than as nesting.
    """
    for label, text in CASES:
        i, open_ = 0, False
        while i < len(text) - 1:
            if text[i] == "*" and text[i + 1] == "*":
                open_ = not open_
                i += 2
                continue
            i += 1
        assert not open_, "%s: unclosed ** in %r" % (label, text[:80])


# --------------------------------------------------- the real /status body

def status_body(granted=(), denied=(), who="none - send /login",
                avail=938.0, can_launch=True):
    """Rebuild the /status text exactly as on_status() assembles it."""
    return (
        "**Relay status**\n\n"
        "**Site:** `%s`\n"
        "**Signing in as:** %s\n"
        "**Access:** %s\n"
        "**Browser:** headless=%s, launched per Screenshot\n"
        "**Memory:** %.0fMB usable (browser needs ~%dMB) — can launch: **%s**\n"
        "**Submissions recorded:** %d\n"
        "**Chats with access:** %s\n"
        "**Chats denied:** %s\n"
        "**Pending confirmations:** %d"
        % (config.SITE_BASE, who, access.describe(markdown=False),
           config.HEADLESS, avail, memory.BROWSER_PEAK_MB,
           "yes" if can_launch else "NO", 3,
           ", ".join("`%s` (%s)" % (c, h) for c, h in granted) or "none",
           ", ".join("`%s`" % (c,) for c in denied) or "none", 0)
    )


def test_the_status_body_parses():
    text = status_body(granted=[("1", "claim")])
    assert text.count("*") % 2 == 0
    assert text.count("`") % 2 == 0


def test_the_status_body_parses_with_several_chats():
    """Two bold labels on one line are what produced the original failure."""
    text = status_body(granted=[("1", "claim"), ("2", "secret:alice")],
                       denied=[("3", "spam")])
    assert text.count("*") % 2 == 0
    assert text.count("`") % 2 == 0
    assert "secret:alice" in text


def test_describe_carries_its_own_markers_by_default():
    """The default is for standalone use, e.g. a log line."""
    assert "**" in access.describe()
    assert "**" not in access.describe(markdown=False)


def test_the_plain_describe_has_balanced_markers_of_its_own():
    plain = access.describe(markdown=False)
    assert plain.count("*") == 0


@pytest.mark.parametrize("mode", ["whitelist_claim", "blacklist", "shared_secret"])
def test_every_mode_produces_a_parseable_describe(monkeypatch, tmp_path, mode, db):
    from conftest import reload_with
    reload_with(monkeypatch, ACCESS_MODE=mode, RELAY_STATE_DIR=str(tmp_path))
    import importlib
    acc = importlib.import_module("relay.telegram.access")
    from relay.store import db
    db.init()
    for md in (True, False):
        text = acc.describe(markdown=md)
        assert text.count("*") % 2 == 0
        assert text.count("`") % 2 == 0


# ------------------------------------------------------------- the greeting

def test_the_greeting_addresses_the_person():
    assert words.welcome("Wai Gie").startswith("👋 Hello, Wai Gie!")


def test_the_greeting_copes_with_no_name():
    text = words.welcome(None)
    assert text.startswith("👋 Hello!")
    assert "%" not in text.split("\n")[0]


def test_the_greeting_has_no_formatting_artifacts():
    """It once printed the dict itself, because %s was given a dict."""
    text = words.welcome("Wai Gie")
    assert "{'" not in text
    assert "'name'" not in text