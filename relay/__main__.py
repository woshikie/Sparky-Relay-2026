"""Entry point: python -m relay."""
import sys

from relay import errors

# config validates ACCESS_MODE at import time, and everything else imports it.
# Doing it first, in a try, means a bad mode prints the message telling the
# operator which line of secrets.env to fix -- rather than a traceback that
# buries it under import frames.
try:
    from relay import config  # noqa: F401  (imported for its validation)
except errors.ConfigRefused as _exc:
    sys.stderr.write("%s\n" % _exc)
    sys.exit(2)

from relay.telegram.app import main

if __name__ == "__main__":
    main()
