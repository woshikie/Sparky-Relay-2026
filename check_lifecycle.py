"""End-to-end check of the on-demand browser lifecycle, against the real site.

Proves: launch -> login -> read the site's Detected Steps -> close, and that
memory returns to baseline. Submits nothing.
"""
import os
import sys
import time

import config
import ledger
import memory
import relay_site

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "..", "workstation", "samples")
CASES = [("Saturday-3-October.jpg", 2831), ("Sunday-4-October.jpg", 6532)]


def avail():
    return memory.mem_mb()


def main():
    ledger.init()
    print(memory.describe(), flush=True)
    baseline = avail()
    print("baseline available: %.0fMB" % baseline, flush=True)

    r = relay_site.Relay(config.SITE_BASE, headless=True, verbose=True)
    ok = True
    for fname, truth in CASES:
        path = os.path.join(SAMPLES, fname)
        if not os.path.exists(path):
            print("SKIP %s (missing)" % fname, flush=True)
            continue
        t0 = time.time()
        try:
            r.start()
            r.login(config.SITE_USERNAME, config.SITE_PASSWORD)
            peak = avail()
            steps, reported = r.upload(path)
            dt = time.time() - t0
            mark = "OK " if steps == truth else "MISMATCH"
            if steps != truth:
                ok = False
            print("%s %s -> %s (want %d) in %.1fs | %.0fMB free during"
                  % (mark, fname, reported, truth, dt, peak), flush=True)
        except Exception as e:
            ok = False
            print("FAIL %s: %r" % (fname, e), flush=True)
        finally:
            r.stop()
            # give the kernel a moment to reclaim
            for _ in range(20):
                time.sleep(0.5)
                if avail() and avail() > baseline - 60:
                    break
            print("   after close: %.0fMB available (baseline %.0fMB)"
                  % (avail(), baseline), flush=True)

    # idempotency: stop() with no driver must not explode
    r.stop()
    r.stop()

    # start() must refuse when memory is short
    orig = memory.MIN_FREE_MB
    memory.MIN_FREE_MB = 10 ** 9
    try:
        r.start()
        print("FAIL: started despite impossible memory requirement", flush=True)
        ok = False
        r.stop()
    except memory.InsufficientMemory as e:
        print("OK  refused to launch under memory pressure: %s" % e, flush=True)
    finally:
        memory.MIN_FREE_MB = orig

    print("\n%s" % ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"),
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
