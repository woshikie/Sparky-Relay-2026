"""Inline-button payloads: one codec for the wire.

Keyboards build opaque strings, handlers split them back apart; both ends
used to inline the "dt:..." shapes, so a typo on either side was a silent
no-op button -- and the PREFIX/handler-regex agreement was held by a test
scraping app.main source. Now the shapes live here: builders encode,
handlers decode through one function, and a malformed payload raises
ValueError instead of indexing thin air.
"""

# The date-picker namespace, matching the ^dt: handler pattern in app.py.
PREFIX = "dt"


def date(kind: str, *parts: object) -> str:
    """dt:today, dt:yday, dt:back, dt:pick:2026:10, dt:day:2026-10-04."""
    return ":".join([PREFIX, kind, *(str(p) for p in parts)])


def confirm(action: str) -> str:
    """ok:go, ok:cancel."""
    return ":".join(["ok", action])


def decode(data: str | None) -> tuple[str, str, list[str]]:
    """Split a payload into (prefix, kind, rest).

    Raises ValueError when malformed -- handlers answer "something went
    wrong" rather than crash on a bare index. The router already guarantees
    the prefix; this guards everything after it.
    """
    if not data:
        raise ValueError("empty callback payload")
    prefix, kind, *rest = data.split(":")
    if not prefix or not kind:
        raise ValueError("malformed callback payload: %r" % (data,))
    return prefix, kind, rest
