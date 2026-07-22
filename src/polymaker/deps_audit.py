"""Dependency audit helpers (T1-07). Pure functions over lock/pyproject/METADATA."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

POST_INSTALL_HINTS = (
    "cmdclass",
    "install_requires",  # alone not suspicious; used with context
    "egg_info",
    "setuptools.command",
    "pip._internal",
)

# entry-point groups that are unusual for a trading bot dependency surface
SUSPICIOUS_EP_GROUPS = (
    "distutils.commands",
    "setuptools.finalize_distribution_options",
)


@dataclass
class PackageAudit:
    name: str
    version: str
    source: str
    has_hash: bool
    direct: bool = False
    pinned_exact: bool | None = None  # only for direct deps from pyproject
    flags: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    packages: list[PackageAudit]
    baseline_bumps: list[dict[str, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no high-severity flags (git/url/missing hash/
        baseline drift / metadata hints). Unpinned direct deps are reported but
        do not fail `ok` — the lockfile is the hash pin."""
        high = [f for f in self.flags if not f.startswith("info:")]
        for p in self.packages:
            for f in p.flags:
                if f.startswith("unpinned_direct:"):
                    continue
                high.append(f"{p.name}:{f}")
        return not high

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "n_packages": len(self.packages),
            "n_flagged_packages": sum(1 for p in self.packages if p.flags),
            "flags": self.flags,
            "baseline_bumps": self.baseline_bumps,
            "packages": [asdict(p) for p in self.packages],
        }


def _parse_pyproject_deps(pyproject: Path) -> dict[str, str]:
    """name -> version specifier for project + optional-deps."""
    data = tomllib.loads(pyproject.read_text())
    out: dict[str, str] = {}
    for item in data.get("project", {}).get("dependencies") or []:
        name, spec = _split_req(str(item))
        out[name.lower()] = spec
    for _extra, items in (data.get("project", {}).get("optional-dependencies") or {}).items():
        for item in items:
            name, spec = _split_req(str(item))
            out.setdefault(name.lower(), spec)
    return out


def _split_req(req: str) -> tuple[str, str]:
    # strip env markers
    req = req.split(";")[0].strip()
    m = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", req)
    if not m:
        return req.lower(), ""
    return m.group(1).lower().replace("_", "-"), m.group(2).strip()


def _source_kind(src: dict[str, Any] | None) -> str:
    if not src:
        return "unknown"
    if "registry" in src:
        return "registry"
    if "path" in src or "editable" in src:
        return "path"
    if "git" in src:
        return "git"
    if "url" in src:
        return "url"
    return "other:" + ",".join(sorted(src.keys()))


def _has_artifact_hash(pkg: dict[str, Any]) -> bool:
    sdist = pkg.get("sdist") or {}
    if sdist.get("hash"):
        return True
    return any(w.get("hash") for w in pkg.get("wheels") or [])


def lock_snapshot(lock_path: Path) -> dict[str, dict[str, str]]:
    """name -> {version, source, hash} from uv.lock."""
    data = tomllib.loads(lock_path.read_text())
    snap: dict[str, dict[str, str]] = {}
    for pkg in data.get("package") or []:
        name = str(pkg["name"]).lower()
        hashes: list[str] = []
        sdist = pkg.get("sdist") or {}
        if sdist.get("hash"):
            hashes.append(str(sdist["hash"]))
        for w in pkg.get("wheels") or []:
            if w.get("hash"):
                hashes.append(str(w["hash"]))
        snap[name] = {
            "version": str(pkg.get("version") or ""),
            "source": _source_kind(pkg.get("source")),
            "hash": "|".join(sorted(set(hashes))),
        }
    return snap


def audit_lock(
    lock_path: Path,
    pyproject: Path,
    *,
    baseline_path: Path | None = None,
    site_packages: Path | None = None,
) -> AuditReport:
    data = tomllib.loads(lock_path.read_text())
    direct = _parse_pyproject_deps(pyproject)
    packages: list[PackageAudit] = []
    report_flags: list[str] = []

    for pkg in data.get("package") or []:
        name = str(pkg["name"])
        key = name.lower()
        version = str(pkg.get("version") or "")
        source = _source_kind(pkg.get("source"))
        has_hash = _has_artifact_hash(pkg)
        pa = PackageAudit(
            name=name,
            version=version,
            source=source,
            has_hash=has_hash,
            direct=key in direct,
        )
        if key in direct:
            spec = direct[key]
            pa.pinned_exact = spec.startswith("==")
            if not pa.pinned_exact and source == "registry":
                pa.flags.append(f"unpinned_direct:{spec or '*'}")
        if source == "git":
            pa.flags.append("git_source")
        if source.startswith("url"):
            pa.flags.append("url_source")
        if source == "registry" and not has_hash:
            pa.flags.append("missing_hash")
        # local path for the project itself is expected
        if source == "path" and key != "polymaker":
            pa.flags.append("path_source")
        packages.append(pa)

    # direct deps missing from lock
    locked = {p.name.lower() for p in packages}
    for d in direct:
        if d not in locked and d != "polymaker":
            report_flags.append(f"direct_missing_from_lock:{d}")

    bumps: list[dict[str, str]] = []
    if baseline_path is not None and baseline_path.exists():
        base = json.loads(baseline_path.read_text())
        cur = lock_snapshot(lock_path)
        for name, meta in cur.items():
            if name == "polymaker":
                continue
            old = base.get(name)
            if old is None:
                bumps.append({"name": name, "change": "added", "to": meta["version"]})
                continue
            if old.get("version") != meta["version"]:
                bumps.append({
                    "name": name,
                    "change": "version",
                    "from": old.get("version", ""),
                    "to": meta["version"],
                })
            if old.get("hash") and meta.get("hash") and old["hash"] != meta["hash"]:
                bumps.append({
                    "name": name,
                    "change": "hash",
                    "from": old["hash"][:24] + "…",
                    "to": meta["hash"][:24] + "…",
                })
        for name in base:
            if name not in cur and name != "polymaker":
                bumps.append({"name": name, "change": "removed", "from": base[name].get("version", "")})

    # scan installed METADATA for suspicious post-install hints (optional)
    if site_packages is not None and site_packages.exists():
        # Package allowlist for the `easy_install` substring check. These
        # packages mention `easy_install` in their METADATA (Description,
        # Classifier list, or Requires-Python comments) without running it
        # during install. The high-severity `setuptools.command.install`
        # and `cmdclass` hints are NOT allowlisted — those still flag.
        # Normalize the keys to lowercase + dash form so dist-info
        # `pytest_cov-7.1.0.dist-info` matches the `pytest-cov` entry.
        _EASY_INSTALL_ALLOWLIST: frozenset[str] = frozenset({
            "pytest-cov",
        })
        for dist in site_packages.glob("*.dist-info"):
            meta = dist / "METADATA"
            ep = dist / "entry_points.txt"
            name = dist.name.split("-")[0].lower().replace("_", "-")
            flags: list[str] = []
            if meta.exists():
                text = meta.read_text(errors="replace")
                for hint in ("setuptools.command.install", "cmdclass"):
                    if hint in text:
                        flags.append(f"metadata_hint:{hint}")
                if "easy_install" in text and name not in _EASY_INSTALL_ALLOWLIST:
                    flags.append("metadata_hint:easy_install")
            if ep.exists():
                ep_text = ep.read_text(errors="replace")
                for group in SUSPICIOUS_EP_GROUPS:
                    if group in ep_text:
                        flags.append(f"entry_point:{group}")
            if flags:
                for p in packages:
                    if p.name.lower().replace("_", "-") == name:
                        p.flags.extend(flags)
                        break
                else:
                    report_flags.extend(f"{name}:{f}" for f in flags)

    if bumps:
        report_flags.append(f"baseline_drift:{len(bumps)}")

    return AuditReport(packages=packages, baseline_bumps=bumps, flags=report_flags)


def write_baseline(lock_path: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lock_snapshot(lock_path), indent=2, sort_keys=True) + "\n")
