"""Tests for T1-07 dependency audit."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.deps_audit import audit_lock, lock_snapshot, write_baseline


def _mini_lock(path: Path) -> None:
    path.write_text(
        '''\
version = 1
[[package]]
name = "httpx"
version = "0.28.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example/httpx.tar.gz", hash = "sha256:abc", size = 1 }
wheels = [ { url = "https://example/httpx.whl", hash = "sha256:def", size = 1 } ]

[[package]]
name = "evilgit"
version = "1.0.0"
source = { git = "https://evil.example/repo" }

[[package]]
name = "nohash"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
'''
    )


def _mini_pyproject(path: Path) -> None:
    path.write_text(
        '''\
[project]
name = "x"
dependencies = [
  "httpx>=0.28",
  "py-clob-client-v2==1.0.2",
]
'''
    )


def test_audit_flags_git_missing_hash_and_unpinned(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    py = tmp_path / "pyproject.toml"
    _mini_lock(lock)
    _mini_pyproject(py)
    report = audit_lock(lock, py, baseline_path=None)
    by_name = {p.name: p for p in report.packages}
    assert "git_source" in by_name["evilgit"].flags
    assert "missing_hash" in by_name["nohash"].flags
    assert any(f.startswith("unpinned_direct") for f in by_name["httpx"].flags)
    # unpinned alone wouldn't fail ok, but git + missing_hash do
    assert report.ok is False


def test_baseline_detects_version_bump(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    py = tmp_path / "pyproject.toml"
    _mini_lock(lock)
    _mini_pyproject(py)
    base = tmp_path / "baseline.json"
    write_baseline(lock, base)
    # bump httpx
    text = lock.read_text().replace('version = "0.28.0"', 'version = "0.29.0"', 1)
    lock.write_text(text)
    report = audit_lock(lock, py, baseline_path=base)
    assert any(b.get("name") == "httpx" and b.get("change") == "version" for b in report.baseline_bumps)


def test_lock_snapshot_has_hashes(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    _mini_lock(lock)
    snap = lock_snapshot(lock)
    assert "sha256:abc" in snap["httpx"]["hash"]
    assert snap["httpx"]["version"] == "0.28.0"
