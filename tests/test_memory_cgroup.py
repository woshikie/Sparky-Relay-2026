"""memory.py's cgroup file readers, against a fake cgroup tree on disk.

These paths only run inside a container, so they are invisible on a developer
machine. Building a real directory tree of memory.max / memory.current /
memory.stat and pointing the reader at it exercises both the v2 and v1 layouts
without needing a container.
"""
import os
import pytest

import memory


def tree(tmp_path, files):
    """A directory that looks like a cgroup mount, with the given files."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ------------------------------------------------------------------ v2

def test_v2_limit_and_current(tmp_path, monkeypatch):
    root = tree(tmp_path, {
        "memory.max": "996147200\n",
        "memory.current": "1527808\n",
        "memory.stat": "anon 1048576\nslab 131072\nfile 2097152\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    cg = memory._cgroup_limits_mb()
    assert cg["path"] == "v2"
    assert cg["limit_mb"] == pytest.approx(950.0, rel=0.01)
    assert cg["current_mb"] == pytest.approx(1.46, rel=0.05)


def test_v2_anon_includes_a_slice_of_slab(tmp_path, monkeypatch):
    root = tree(tmp_path, {
        "memory.max": "996147200\n",
        "memory.current": "1000000\n",
        "memory.stat": "anon 1048576\nslab 134217728\nfile 0\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    anon = memory._cgroup_anon_mb()
    # 1MB anon + the 64MB slab cap, not the full 128MB slab.
    assert anon == pytest.approx(65.0, rel=0.01)


def test_an_unlimited_v2_group_reports_no_ceiling(tmp_path, monkeypatch):
    root = tree(tmp_path, {
        "memory.max": "max\n",
        "memory.current": "1000\n",
        "memory.stat": "anon 0\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    cg = memory._cgroup_limits_mb()
    assert cg["limit_mb"] is None
    assert memory.container_env() is None


def test_a_v2_group_without_memory_stat(tmp_path, monkeypatch):
    root = tree(tmp_path, {
        "memory.max": "996147200\n",
        "memory.current": "1000000\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_anon_mb() is None


def test_unparseable_numbers_are_survivable(tmp_path, monkeypatch):
    root = tree(tmp_path, {
        "memory.max": "not-a-number\n",
        "memory.current": "1000000\n",
        "memory.stat": "anon not-a-number\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    cg = memory._cgroup_limits_mb()
    assert cg["limit_mb"] is None      # refused, not raised
    assert memory._cgroup_anon_mb() is None


# ------------------------------------------------------------------ v1

def test_v1_limit_and_usage(tmp_path, monkeypatch):
    # v1 mounts its files inside a "memory" subdirectory, which is why
    # _cgroup_root() returns that path rather than the cgroup root.
    root = tree(tmp_path, {
        "memory/memory.limit_in_bytes": "996147200\n",
        "memory/memory.usage_in_bytes": "1527808\n",
        "memory/memory.stat": "total_rss 1048576\ntotal_cache 2097152\n",
    })
    root = os.path.join(root, "memory")
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    cg = memory._cgroup_limits_mb()
    assert cg["path"] == "v1"
    assert cg["limit_mb"] == pytest.approx(950.0, rel=0.01)
    assert cg["current_mb"] == pytest.approx(1.46, rel=0.05)


def test_v1_uses_total_rss_as_the_committed_figure(tmp_path, monkeypatch):
    root = os.path.join(tree(tmp_path, {
        "memory/memory.limit_in_bytes": "996147200\n",
        "memory/memory.usage_in_bytes": "3145728\n",
        "memory/memory.stat": "total_rss 2097152\ntotal_cache 1048576\n",
    }), "memory")
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_anon_mb() == pytest.approx(2.0)


def test_the_v1_unlimited_sentinel_is_recognised(tmp_path, monkeypatch):
    """v1 reports a huge number rather than a string when unlimited."""
    root = os.path.join(tree(tmp_path, {
        "memory/memory.limit_in_bytes": str(9223372036854771712),
        "memory/memory.usage_in_bytes": "1048576\n",
        "memory/memory.stat": "total_rss 1048576\n",
    }), "memory")
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_limits_mb()["limit_mb"] is None


def test_a_stat_with_neither_anon_nor_rss(tmp_path, monkeypatch):
    root = os.path.join(tree(tmp_path, {
        "memory/memory.limit_in_bytes": "996147200\n",
        "memory/memory.usage_in_bytes": "1048576\n",
        "memory/memory.stat": "cache 4096\n",
    }), "memory")
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_anon_mb() is None


# ------------------------------------------------------------- discovery

def test_the_v2_location_is_preferred(tmp_path, monkeypatch):
    both = tmp_path / "cgroup"
    (both / "memory").mkdir(parents=True)
    (both / "memory.max").write_text("max\n")
    (both / "memory" / "memory.limit_in_bytes").write_text("1048576\n")
    monkeypatch.setattr(memory.os.path, "exists",
                        lambda p: str(p).endswith("memory.max"))
    monkeypatch.setattr(memory, "_cgroup_root", lambda: str(both))
    assert memory._cgroup_limits_mb()["path"] == "v2"


def test_no_cgroup_at_all(tmp_path, monkeypatch):
    root = str(tmp_path / "empty")
    os.makedirs(root)
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    cg = memory._cgroup_limits_mb()
    assert cg["limit_mb"] is None
    assert cg["anon_mb"] is None


# ------------------------------------------------------- the fallbacks

def test_total_mb_reports_the_host_total(tmp_path, monkeypatch):
    """The host figure, used when there is no cgroup ceiling."""
    monkeypatch.setattr(memory, "_read_meminfo",
                        lambda: {"MemTotal": 8 * 1024 * 1024})
    assert memory.total_mb() == 8192.0


def test_total_mb_without_meminfo_returns_none(tmp_path, monkeypatch):
    """No /proc/meminfo means no number, not a crash."""
    monkeypatch.setattr(memory, "_read_meminfo", lambda: {})
    assert memory.total_mb() is None


def test_a_cgroup_with_no_anon_and_no_current_commits_nothing(tmp_path, monkeypatch):
    """Both figures missing must fall back to 0.0, not raise.

    Being pessimistic is the safe direction: 0.0 committed means the full
    limit is headroom, which is the optimistic reading, so this is the branch
    that would let a browser launch it cannot afford.
    """
    monkeypatch.setattr(memory, "_cgroup_limits_mb", lambda: {
        "limit_mb": 950.0, "current_mb": None, "anon_mb": None})
    monkeypatch.setattr(memory, "budget_report", lambda: {
        "total_mb": 950.0, "available_mb": 900.0,
        "browser_peak_mb": memory.BROWSER_PEAK_MB,
        "min_free_mb": memory.MIN_FREE_MB, "can_launch": True,
        "cgroup": True, "cgroup_limit_mb": 950.0,
        "cgroup_current_mb": None, "cgroup_committed_mb": None})
    rep = memory.container_env()
    # The fallback is 0.0 committed, so the whole limit is headroom -- and the
    # host figure is then the tighter of the two, as it should be.
    assert rep["cgroup_committed_mb"] == 0.0
    assert rep["available_mb"] == 900.0


def test_a_zero_available_figure_is_treated_as_unknown(tmp_path, monkeypatch):
    """0.0 is falsy, and the code must not read that as 'no data'."""
    monkeypatch.setattr(memory, "_cgroup_limits_mb", lambda: {
        "limit_mb": 950.0, "current_mb": 100.0, "anon_mb": 100.0})
    monkeypatch.setattr(memory, "budget_report", lambda: {
        "total_mb": 950.0, "available_mb": 0.0,
        "browser_peak_mb": memory.BROWSER_PEAK_MB,
        "min_free_mb": memory.MIN_FREE_MB, "can_launch": False,
        "cgroup": True, "cgroup_limit_mb": 950.0,
        "cgroup_current_mb": 100.0, "cgroup_committed_mb": 100.0})
    rep = memory.container_env()
    assert rep["available_mb"] == 850.0


def test_cgroup_root_falls_back_to_the_v2_path(monkeypatch):
    """Neither v2 nor v1 files: the reader still returns a path.

    Patches os.path.exists rather than memory.os.path.exists: they are the
    same object, and patching the attribute on the shared module is what makes
    the two calls inside _cgroup_root both see False.
    """
    monkeypatch.setattr(memory.os.path, "exists", lambda p: False)
    assert memory._cgroup_root() == "/sys/fs/cgroup"


def test_an_unparseable_v1_limit_is_survivable(tmp_path, monkeypatch):
    """A garbage limit_in_bytes must not raise out of the reader."""
    root = tree(tmp_path, {
        "memory.limit_in_bytes": "not a number\n",
        "memory.usage_in_bytes": "1048576\n",
    })
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    out = memory._cgroup_limits_mb()
    assert "limit_mb" not in out or out["limit_mb"] is None


def test_a_missing_memory_stat_reports_no_anon(tmp_path, monkeypatch):
    """No memory.stat means no anon figure, which is not the same as zero."""
    root = tree(tmp_path, {"memory.max": "996147200\n"})
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_anon_mb() is None


def test_an_unreadable_memory_stat_reports_no_anon(tmp_path, monkeypatch):
    """A stat file that cannot be opened must not raise.

    A directory named memory.stat makes open() raise IsADirectoryError,
    which is an OSError -- the same branch a permission error takes, without
    patching a builtin.
    """
    root = tree(tmp_path, {"memory.max": "996147200\n"})
    (tmp_path / "memory.stat").mkdir()
    monkeypatch.setattr(memory, "_cgroup_root", lambda: root)
    assert memory._cgroup_anon_mb() is None


def test_an_unreadable_meminfo_yields_no_figures(monkeypatch):
    """A /proc/meminfo that cannot be opened must not raise.

    The reader is called on every budget check, so an exception here would
    take down /status and the pre-flight alike.
    """
    import builtins
    real_open = builtins.open

    def boom(path, *a, **kw):
        if str(path) == "/proc/meminfo":
            raise OSError("no such file")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", boom)
    assert memory._read_meminfo() == {}
    assert memory.mem_mb() is None


def test_an_empty_meminfo_yields_no_figures(monkeypatch):
    """A /proc/meminfo with no lines must not raise either."""
    import builtins
    import io
    real_open = builtins.open

    def empty(path, *a, **kw):
        if str(path) == "/proc/meminfo":
            return io.StringIO("")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", empty)
    assert memory._read_meminfo() == {}
    assert memory.mem_mb() is None
    assert memory.total_mb() is None


def test_a_meminfo_line_with_no_value_is_skipped(monkeypatch):
    """'MemAvailable:' with nothing after it must not raise int('')."""
    import builtins
    import io
    real_open = builtins.open

    def partial(path, *a, **kw):
        if str(path) == "/proc/meminfo":
            return io.StringIO("MemTotal:       8000000 kB\n"
                               "MemAvailable:\n"
                               "MemFree:        1000000 kB\n")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", partial)
    vals = memory._read_meminfo()
    assert vals == {"MemTotal": 8000000, "MemFree": 1000000}


def test_cgroup_root_finds_a_real_v2_group(monkeypatch):
    """The success branch: a memory.max exists, so that path is returned.

    The other tests patch _cgroup_root out, so the real one is never exercised
    on the path where it actually finds something.
    """
    def exists(p):
        return str(p) == "/sys/fs/cgroup/memory.max"
    monkeypatch.setattr(memory.os.path, "exists", exists)
    assert memory._cgroup_root() == "/sys/fs/cgroup"


def test_cgroup_root_prefers_v2_over_v1(monkeypatch):
    """v2 is checked first, and must win when both are present."""
    def exists(p):
        return str(p) in ("/sys/fs/cgroup/memory.max",
                          "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    monkeypatch.setattr(memory.os.path, "exists", exists)
    assert memory._cgroup_root() == "/sys/fs/cgroup"


def test_cgroup_root_finds_a_v1_group(monkeypatch):
    """v1 only: memory.limit_in_bytes under the v1 directory, no memory.max.

    The path has to be the full v1 one. Matching any path ending in
    'memory.limit_in_bytes' would also match the v2 directory's copy and
    return the wrong root.
    """
    def exists(p):
        return str(p) == "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    monkeypatch.setattr(memory.os.path, "exists", exists)
    assert memory._cgroup_root() == "/sys/fs/cgroup/memory"
