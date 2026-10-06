"""Exception types the app defines.

They live here rather than in config.py because config.py validates its
environment at import time: importing it to get at an exception class would
raise for anyone whose ACCESS_MODE is unset. That is exactly when you might
want to catch it.
"""


class ConfigRefused(Exception):
    """The configuration is unusable, with the message for the operator.

    A named exception rather than a bare SystemExit so it is assertable in
    tests and catchable by a wrapper that wants to add context. bot.sh turns it
    into a plain printed error and a non-zero exit, which is what a person
    running the bot actually needs to see.
    """
