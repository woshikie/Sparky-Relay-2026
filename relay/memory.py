"""Free memory and a browser lifecycle that fits a 1GB host.

The site's OCR runs ONNX in the page, so a real engine is unavoidable: measured
peak for Firefox on the upload flow is ~640MB. On a 1GB VM the only way that
survives is to not hold it open. Measured alternatives, for the record:

    Lightpanda (Zig)      308MB   -- OCR never runs: no createImageBitmap,
                                     so ocrEngine.js cannot rasterise
    Firefox + geckodriver 640MB   -- works
    Chromium single-proc  794MB   -- works, slower, heavier
    Chromium multi-proc   1060MB  -- works, heaviest

So: launch per Screenshot, close after Commit, and refuse to start if the host
cannot fit it. See docs/adr/0004-browser-lifecycle-on-1gb-host.md.
"""
import os

# Peak observed for the whole Firefox tree while the OCR runs, plus headroom
# for the 16.5MB recognition model to be decoded rather than just mapped.
BROWSER_PEAK_MB = 780

# Refuse to start below this: the OOM killer taking out the bot mid-run is worse
# than telling the user to wait.
MIN_FREE_MB = BROWSER_PEAK_MB

# The bot's own process, roughly. Used to reason about what is left over.
BOT_FOOTPRINT_MB = 60


def _read_meminfo():
    vals = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    vals[k.strip()] = int(parts[0])  # kB
    except OSError:
        pass
    return vals


def mem_mb(field="MemAvailable"):
    v = _read_meminfo().get(field)
    return None if v is None else v / 1024.0


def total_mb():
    v = _read_meminfo().get("MemTotal")
    return None if v is None else v / 1024.0


def budget_report():
    info = _read_meminfo()
    total = info.get("MemTotal", 0) / 1024.0
    avail = info.get("MemAvailable", 0) / 1024.0
    cg = _cgroup_limits_mb()
    return {
        "total_mb": total,
        "available_mb": avail,
        "browser_peak_mb": BROWSER_PEAK_MB,
        "min_free_mb": MIN_FREE_MB,
        "can_launch": avail >= MIN_FREE_MB if avail else None,
        "cgroup": cg["limit_mb"] is not None,
        "cgroup_limit_mb": cg["limit_mb"],
        "cgroup_current_mb": cg.get("current_mb"),
        "cgroup_committed_mb": cg.get("anon_mb"),
    }


class InsufficientMemory(Exception):
    """Not enough free RAM to run the browser safely right now."""

    def __init__(self, report):
        self.report = report
        super().__init__(
            "need %dMB free to run the browser, have %dMB"
            % (report["min_free_mb"], int(report["available_mb"] or 0)))


def require_memory():
    """Raise rather than start a browser that is going to get OOM-killed."""
    rep = budget()
    # Missing data means we cannot reason about it; do not block.
    if rep["available_mb"] and rep["available_mb"] < MIN_FREE_MB:
        raise InsufficientMemory(rep)
    return rep


def container_env():
    """The memory budget that actually applies inside a cgroup, or None.

    Three things have to be reconciled:

    1. The host's MemAvailable is irrelevant in a capped container -- it would
       report 16GB free and let us start a browser that gets OOM-killed 30s in.
    2. cgroup `memory.current` is *not* the real cost either. It includes file
       cache (the browser's profile, fonts, the ONNX model on disk), which the
       kernel reclaims under pressure without complaint. Measuring that as
       committed made the pre-flight refuse to launch with plenty of headroom.
    3. So the number that matters is limit MINUS unreclaimable memory (anon
       plus a slice of slab), floored by the host's own headroom.
    """
    cg = _cgroup_limits_mb()
    if cg["limit_mb"] is None:
        return None
    rep = budget_report()

    committed = cg.get("anon_mb")
    if committed is None:
        # No memory.stat: fall back to current, which over-counts. Being
        # pessimistic here is the safe direction to fail.
        committed = cg.get("current_mb")
    if committed is None:
        committed = 0.0
    headroom = max(0.0, cg["limit_mb"] - committed)

    effective = headroom
    if rep["available_mb"]:
        effective = min(effective, rep["available_mb"])

    return {
        "total_mb": cg["limit_mb"],
        "available_mb": effective,
        "browser_peak_mb": BROWSER_PEAK_MB,
        "min_free_mb": MIN_FREE_MB,
        "can_launch": effective >= MIN_FREE_MB,
        "cgroup": True,
        "cgroup_limit_mb": cg["limit_mb"],
        "cgroup_current_mb": cg.get("current_mb"),
        "cgroup_committed_mb": committed,
    }


def budget():
    """The budget that actually applies: cgroup ceiling if capped, else host."""
    return container_env() or budget_report()


MB = 1024.0 * 1024.0


def _cgroup_root():
    """Where this process's memory files live (cgroup v2 and v1 differ)."""
    for p in ("/sys/fs/cgroup", "/sys/fs/cgroup/memory"):
        if os.path.exists(os.path.join(p, "memory.max")) or \
           os.path.exists(os.path.join(p, "memory.limit_in_bytes")):
            return p
    return "/sys/fs/cgroup"


def _cgroup_limits_mb():
    """The cgroup memory ceiling and committed usage for this process, in MB.

    Reports `current` (all charged memory) and `anon` (memory that cannot be
    reclaimed). See container_env for why only the second one is a real cost.
    """
    out = {"limit_mb": None, "current_mb": None, "anon_mb": None, "path": None}
    base = _cgroup_root()

    # cgroup v2
    if os.path.exists(os.path.join(base, "memory.max")):
        out["path"] = "v2"
        try:
            with open(os.path.join(base, "memory.max")) as f:
                raw = f.read().strip()
            if raw != "max":
                out["limit_mb"] = int(raw) / MB
            with open(os.path.join(base, "memory.current")) as f:
                out["current_mb"] = int(f.read().strip()) / MB
        except (OSError, ValueError):
            pass
        out["anon_mb"] = _cgroup_anon_mb()
        return out

    # cgroup v1
    if os.path.exists(os.path.join(base, "memory.limit_in_bytes")):
        out["path"] = "v1"
        try:
            with open(os.path.join(base, "memory.limit_in_bytes")) as f:
                lim = int(f.read().strip())
            if lim < (1 << 62):      # v1 sentinel meaning "unlimited"
                out["limit_mb"] = lim / MB
            with open(os.path.join(base, "memory.usage_in_bytes")) as f:
                out["current_mb"] = int(f.read().strip()) / MB
        except (OSError, ValueError):
            pass
        out["anon_mb"] = _cgroup_anon_mb()
    return out


def _cgroup_anon_mb():
    """Unreclaimable memory charged to this cgroup, in MB, or None.

    v2 exposes it as memory.stat's `anon`. v1 splits it across `total_rss` and
    part of `total_cache`; RSS is the honest proxy there.
    """
    base = _cgroup_root()
    stat = os.path.join(base, "memory.stat")
    if not os.path.exists(stat):
        return None
    try:
        with open(stat) as f:
            raw = f.read()
    except OSError:
        return None
    vals = {}
    for line in raw.splitlines():
        k, _, v = line.partition(" ")
        try:
            vals[k] = int(v)
        except ValueError:
            continue
    if "anon" in vals:
        # slab is technically reclaimable but slow to evict, so treat a slice
        # of it as committed rather than free.
        return (vals["anon"] + min(vals.get("slab", 0), 64 * MB)) / MB
    if "total_rss" in vals:
        return vals["total_rss"] / MB
    return None


def tune_firefox_env():
    """Keep the engine from over-allocating on a small host.

    Best-effort: these are preferences, not guarantees, and a Firefox that
    ignores one is still correct, just heavier.
    """
    return {
        # Do not keep a large idle cache around between runs.
        "browser.cache.disk.enable": False,
        "browser.cache.memory.enable": False,
        "browser.sessionhistory.max_total_viewers": 0,
        # No background chatter while we wait on the page.
        "browser.shell.checkDefaultBrowser": False,
        "toolkit.telemetry.enabled": False,
        "datareporting.healthreport.uploadEnabled": False,
        "app.update.enabled": False,
        "extensions.update.enabled": False,
        "network.prefetch-next": False,
        "network.dns.disablePrefetch": True,
        "browser.safebrowsing.malware.enabled": False,
        "browser.safebrowsing.phishing.enabled": False,
        "dom.ipc.processCount": 1,
        "fission.autostart": False,
    }


def describe():
    rep = budget()
    ok = "yes" if rep["can_launch"] else "NO"
    if rep.get("cgroup"):
        # Show committed (anon) rather than current: current includes reclaimable
        # file cache and would understate the headroom we actually have.
        return ("RAM budget: %.0fMB usable (cgroup limit %.0fMB, %.0fMB "
                "committed of %.0fMB charged) | browser peak ~%dMB | "
                "can launch: %s"
                % (rep["available_mb"], rep["cgroup_limit_mb"] or 0,
                   rep["cgroup_committed_mb"] or 0,
                   rep["cgroup_current_mb"] or 0, rep["browser_peak_mb"], ok))
    return ("RAM: %.0fMB total, %.0fMB available | browser peak ~%dMB "
            "| can launch: %s"
            % (rep["total_mb"], rep["available_mb"], rep["browser_peak_mb"], ok))
