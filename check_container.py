"""Drive the site from inside the running container, end to end minus Commit.

Runs in the live container so it exercises the same paths the bot does: the
cgroup memory budget, the container detection, and the on-demand browser.
Submits nothing.
"""

import os
import re
import time

from relay import config, memory
from relay.site import driver as relay_site
from relay.store import ledger

DETECT = re.compile(r"Detected steps\s*([\d,. ]+)", re.I)
CASES = [("Saturday-3-October.jpg", 2831), ("Sunday-4-October.jpg", 6532)]


def main():
    print("container detected :", relay_site._in_container(), flush=True)
    print("cgroup budget      :", memory.describe(), flush=True)
    print("ledger             :", ledger.init(), flush=True)
    print("firefox            :", config.FIREFOX_BIN, flush=True)
    print("geckodriver        :", relay_site.GECKO, flush=True)
    print(flush=True)

    ok = True
    r = relay_site.Relay(config.SITE_BASE, headless=True, verbose=True)
    for fname, truth in CASES:
        path = os.path.join("/samples", fname)
        if not os.path.exists(path):
            print("SKIP %s (not mounted)" % fname, flush=True)
            continue
        before = memory.mem_mb()
        t0 = time.time()
        try:
            r.start()
            r.login(config.SITE_USERNAME, config.SITE_PASSWORD)
            steps, reported = r.upload(path)
            during = memory.mem_mb()
            good = steps == truth
            ok = ok and good
            print("\n%s %s" % ("OK  " if good else "BAD ", fname), flush=True)
            print("    site read    : %s (want %d)" % (reported, truth), flush=True)
            print("    elapsed      : %.1fs" % (time.time() - t0), flush=True)
            print(
                "    host free    : %.0fMB before, %.0fMB during"
                % (before or 0, during or 0),
                flush=True,
            )
        except Exception as e:
            ok = False
            print("\nFAIL %s: %r" % (fname, e), flush=True)
        finally:
            r.stop()
        time.sleep(3)
        print("    after close  : %.0fMB free" % (memory.mem_mb() or 0), flush=True)

    print("\n%s" % ("CONTAINER OK" if ok else "CONTAINER PROBLEM"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
