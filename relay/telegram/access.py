"""Who may drive the Relay, under whichever Access Mode is configured.

One entry point — `check(chat_id)` — so every handler asks the same question
the same way, and so changing Access Mode is a config change rather than an
audit of every handler.

    whitelist_claim  the first chat to /start claims the Relay; everyone else
                     needs to be granted. Safest.
    blacklist       open to any chat not denied. This is a public write path.
    shared_secret   must present a per-person Shared Secret at /start.

ACCESS_MODE is mandatory. An unset value exits at import time rather than
defaulting, because the difference between the safest and the most open mode is
who can write to a live leaderboard account.
"""

import time

import relay.store.ledger as ledger
import relay.telegram.words as words
from relay import config


class Decision:
    """The outcome of an access check, with enough detail to tell the user why."""

    def __init__(
        self,
        allowed: bool,
        why: str,
        how: str | None = None,
        retry_after: int = 0,
        chat_id: int | None = None,
    ) -> None:
        self.allowed = allowed
        self.why = why
        self.how = how
        self.retry_after = retry_after
        # Carried so callers can name the chat in their reply without having to
        # thread it through separately.
        self.chat_id = chat_id

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        return "Decision(allowed=%r, why=%r)" % (self.allowed, self.why)


def _denied_by_config(chat_id: int) -> bool:
    """Chats refused by DENY_CHAT_IDS, or already on the deny list."""
    if config.DENY_CHAT_IDS and int(chat_id) in config.DENY_CHAT_IDS:
        return True
    return ledger.is_denied(chat_id)


def check(chat_id: int, username: str | None = None) -> Decision:
    """May this chat drive the Relay? Always call this, never inline the rule."""
    chat_id = int(chat_id)

    if _denied_by_config(chat_id):
        return Decision(False, "denied", chat_id=chat_id)

    existing = ledger.has_access(chat_id)
    if existing:
        ledger.remember_user(chat_id, username)
        return Decision(True, "granted", how=existing, chat_id=chat_id)

    if config.ACCESS_MODE == "blacklist":
        # Open by design. No grant row: access is implicit and re-checking the
        # deny list each time is what makes revocation take effect.
        ledger.remember_user(chat_id, username)
        return Decision(True, "blacklist", chat_id=chat_id)

    if config.ACCESS_MODE == "whitelist_claim":
        return Decision(False, "needs_claim", chat_id=chat_id)

    if config.ACCESS_MODE == "shared_secret":
        if ledger.secret_throttled(chat_id):
            wait = ledger.SECRET_WINDOW - (time.time() - _last_try(chat_id))
            return Decision(
                False, "secret_throttled", chat_id=chat_id, retry_after=int(wait) + 1
            )
        return Decision(False, "needs_secret", chat_id=chat_id)

    # Unreachable: config validates the mode at import.
    return Decision(False, "unknown_mode", chat_id=chat_id)


def _last_try(chat_id: int) -> float:
    return ledger.last_secret_attempt(chat_id)


def claim(chat_id: int, username: str | None = None) -> bool:
    """First-run claim. Returns True if this chat just claimed it.

    One statement: the INSERT only fires when the table is empty, so two
    simultaneous first /starts cannot both be told they won. (In today's
    single event loop the old SELECT-then-INSERT could not interleave; this
    is simpler as well as safer.)
    """
    claimed = ledger.claim_if_empty(chat_id, "claim", ledger.now())
    if claimed:
        ledger.remember_user(chat_id, username)
    return claimed


def present_secret(
    chat_id: int, candidate: str, username: str | None = None
) -> Decision:
    """Check a Shared Secret. Returns a Decision.

    The rate limit is enforced here, at the single point where a candidate is
    evaluated, rather than in the handler. A caller that forgot to check first
    would otherwise be able to guess at Telegram's message rate, and the rule
    would only be as good as its most recent call site.

    A correct secret during a wait is still refused. That is mildly
    inconvenient, and it is the point: it makes the limit a property of the
    function rather than something each caller has to remember.

    Rate-limited rather than locked out, deliberately: a lockout would let
    anyone who knows a chat id keep the owner out of their own bot, which is
    worse than a slow guessing attack on a passphrase.
    """
    if ledger.secret_throttled(chat_id):
        wait = ledger.SECRET_WINDOW - (
            time.time() - ledger.last_secret_attempt(chat_id)
        )
        return Decision(
            False, "secret_throttled", chat_id=chat_id, retry_after=int(wait) + 1
        )
    label = ledger.check_secret(candidate)
    if label:
        ledger.note_secret_attempt(chat_id, ok=True)
        ledger.grant(chat_id, "secret:%s" % label)
        ledger.remember_user(chat_id, username)
        return Decision(True, "granted", how="secret:%s" % label)
    ledger.note_secret_attempt(chat_id, ok=False)
    return Decision(False, "secret_wrong", chat_id=chat_id)


def grant(chat_id: int, how: str = "manual") -> bool:
    ledger.grant(chat_id, how)
    return True


def revoke(chat_id: int) -> bool:
    ledger.revoke(chat_id)
    return True


def deny(chat_id: int, reason: str = "") -> bool:
    ledger.deny(chat_id, reason)
    return True


def undeny(chat_id: int) -> bool:
    ledger.undeny(int(chat_id))
    return True


def describe(markdown: bool = True) -> str:
    """Human summary for /status.

    `markdown=False` returns a plain fragment for embedding inside a bold span.
    The default carries its own ** markers, and nesting those inside another
    ** pair is what made /status fail to parse -- the reply was silently
    dropped, so the command looked like it did nothing.

    The mode name is escaped because `whitelist_claim` contains an underscore,
    which Telegram reads as an italic delimiter.
    """
    mode = words.md(config.ACCESS_MODE)
    template = "Access Mode: **%s**" if markdown else "Access Mode: %s"
    line = template % mode
    if config.ACCESS_MODE == "blacklist":
        line += "  ⚠️ anyone not denied can drive this"
    if config.ACCESS_MODE == "whitelist_claim":
        line += "  (first /start claims it)"
    if config.ACCESS_MODE == "shared_secret":
        line += "  (%d secret(s) configured)" % len(ledger.list_secret_labels())
    return line
