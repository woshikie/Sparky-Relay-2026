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
