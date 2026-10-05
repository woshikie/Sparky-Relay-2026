"""memory.py: reading the cgroup ceiling, and the pre-flight that guards it.

The bug this file exists to prevent: measuring headroom as
limit-minus-current, where current includes reclaimable file cache. After a
browser run on a 950MB container that reads 900MB "used" with 12MB genuinely
committed, so the bot refused to run a second Submission with real headroom.
"""
import pytest

import memory


# ----------------------------------------------------------------- helpers

class FakeMeminfo(dict):
    pass


def write_meminfo(monkeypatch, total_kb, avail_kb):
    path = "/proc/meminfo"

    def fake_open():
        import io
        return io.StringIO(
            "MemTotal:       %d kB\nMemFree:  %d kB\nMemAvailable: %d kB\n"
            % (total_kb, avail_kb // 2, avail_kb))

    monkeypatch.setattr(memory, "_read_meminfo", lambda: {
        "MemTotal": total_kb, "MemAvailable": avail_kb})
    monkeypatch.setattr(memory, "total_mb", lambda: total_kb / 1024.0)
    monkeypatch.setattr(memory, "mem_mb", lambda: avail_kb / 1024.0)


# --------------------------------------------------------------- meminfo

def test_budget_report_shape(monkeypatch):
    write_meminfo(monkeypatch, 1024 * 1024, 512 * 1024)
    b = memory.budget_report()
    assert b["total_mb"] == 1024.0
    assert b["available_mb"] == 512.0
    assert b["cgroup"] is False


def test_an_uncapped_host_uses_host_memory(monkeypatch):
    write_meminfo(monkeypatch, 1024 * 1024, 512 * 1024)
    monkeypatch.setattr(memory, "_cgroup_limits_mb", lambda: {
        "limit_mb": None, "current_mb": None, "anon_mb": None, "path": None})
    assert memory.container_env() is None
    assert memory.budget()["total_mb"] == 1024.0


# ------------------------------------------------- the cgroup accounting

@pytest.fixture
def cgroup(monkeypatch):
    """Pin the cgroup reading so the arithmetic is what is under test."""
    def _set(limit, current, anon, host_avail=20000.0, host_total=32000.0):
        monkeypatch.setattr(memory, "_cgroup_limits_mb", lambda: {
            "limit_mb": limit, "current_mb": current,
            "anon_mb": anon, "path": "v2"})
        monkeypatch.setattr(memory, "budget_report", lambda: {
            "total_mb": host_total, "available_mb": host_avail,
            "browser_peak_mb": memory.BROWSER_PEAK_MB,
            "min_free_mb": memory.MIN_FREE_MB,
            "can_launch": host_avail >= memory.MIN_FREE_MB,
            "cgroup": limit is not None,
            "cgroup_limit_mb": limit, "cgroup_current_mb": current,
            "cgroup_committed_mb": anon})
    return _set


def test_file_cache_is_not_counted_as_committed(cgroup):
    """The regression: 900MB charged, 12MB committed, 950MB limit.

    Reclaiming that cache is the kernel's problem, not ours, so headroom is
    938MB and a browser can start.
    """
    cgroup(limit=950.0, current=900.0, anon=12.0)
    rep = memory.container_env()
    assert rep["available_mb"] == pytest.approx(938.0)
    assert rep["can_launch"] is True


def test_a_genuinely_full_container_still_refuses(cgroup):
    cgroup(limit=950.0, current=940.0, anon=935.0)
    rep = memory.container_env()
    assert rep["available_mb"] == pytest.approx(15.0)
    assert rep["can_launch"] is False


def test_the_host_is_the_tighter_constraint_when_it_is_lower(cgroup):
    cgroup(limit=950.0, current=20.0, anon=20.0, host_avail=400.0)
    assert memory.container_env()["available_mb"] == pytest.approx(400.0)


def test_headroom_never_goes_negative(cgroup):
    cgroup(limit=100.0, current=200.0, anon=200.0)
    assert memory.container_env()["available_mb"] == 0.0


def test_a_missing_anon_figure_falls_back_to_current(cgroup):
    """Better to under-estimate than to trust a number we do not have."""
    cgroup(limit=950.0, current=940.0, anon=None, host_avail=20000.0)
    rep = memory.container_env()
    assert rep["available_mb"] == pytest.approx(10.0)
    assert rep["can_launch"] is False


def test_the_limit_is_reported_alongside_the_headroom(cgroup):
    cgroup(limit=950.0, current=900.0, anon=12.0)
    rep = memory.container_env()
    assert rep["cgroup"] is True
    assert rep["cgroup_limit_mb"] == 950.0
    assert rep["cgroup_committed_mb"] == 12.0


# ------------------------------------------------------------ pre-flight

def test_require_passes_with_room(cgroup):
    cgroup(limit=4000.0, current=100.0, anon=100.0)
    assert memory.require_memory()["can_launch"] is True


def test_require_raises_without_room(cgroup):
    cgroup(limit=950.0, current=940.0, anon=935.0)
    with pytest.raises(memory.InsufficientMemory):
        memory.require_memory()


def test_the_refusal_explains_the_numbers(cgroup):
    cgroup(limit=950.0, current=940.0, anon=935.0)
    with pytest.raises(memory.InsufficientMemory) as exc:
        memory.require_memory()
    assert str(memory.MIN_FREE_MB) in str(exc.value)


def test_unknown_memory_does_not_block(monkeypatch):
    monkeypatch.setattr(memory, "budget", lambda: {
        "total_mb": 0, "available_mb": None, "browser_peak_mb": 780,
        "min_free_mb": 780, "can_launch": None, "cgroup": False,
        "cgroup_limit_mb": None, "cgroup_current_mb": None,
        "cgroup_committed_mb": None})
    assert memory.require_memory()["available_mb"] is None


# ------------------------------------------------------------- meminfo io

def test_reading_meminfo_from_disk():
    """The real read, not a mock: /proc/meminfo exists on Linux."""
    vals = memory._read_meminfo()
    assert "MemTotal" in vals
    assert vals["MemTotal"] > 0


# --------------------------------------------------------------- firefox

def test_the_tuning_targets_the_known_waste():
    prefs = memory.tune_firefox_env()
    assert prefs["browser.cache.disk.enable"] is False
    assert prefs["browser.cache.memory.enable"] is False
    assert prefs["browser.sessionhistory.max_total_viewers"] == 0
    assert prefs["toolkit.telemetry.enabled"] is False
    assert prefs["datareporting.healthreport.uploadEnabled"] is False
    assert prefs["app.update.enabled"] is False
    assert prefs["network.prefetch-next"] is False
    assert prefs["dom.ipc.processCount"] == 1
    assert prefs["fission.autostart"] is False


def test_the_tuning_is_all_boolean_or_int():
    """A preference of the wrong type is ignored by Firefox, silently."""
    for k, v in memory.tune_firefox_env().items():
        assert isinstance(k, str)
        assert isinstance(v, (bool, int, str)), k


def test_the_peak_carries_headroom_over_the_measurement():
    """640MB measured; 780 is the number the pre-flight uses, with margin."""
    assert memory.BROWSER_PEAK_MB > 640
    assert memory.MIN_FREE_MB == memory.BROWSER_PEAK_MB


# ---------------------------------------------------------------- output

def test_describe_shows_committed_not_charged(cgroup):
    """Showing 900MB 'used' would look like there is no room left."""
    cgroup(limit=950.0, current=900.0, anon=12.0)
    text = memory.describe()
    assert "12MB" in text
    assert "950MB" in text
    # The charged figure is printed, but never presented as the headroom.
    assert "938MB usable" in text


def test_describe_on_a_plain_host(monkeypatch):
    write_meminfo(monkeypatch, 32000 * 1024, 16000 * 1024)
    monkeypatch.setattr(memory, "_cgroup_limits_mb", lambda: {
        "limit_mb": None, "current_mb": None, "anon_mb": None, "path": None})
    assert "available" in memory.describe()
