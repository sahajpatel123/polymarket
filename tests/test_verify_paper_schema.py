"""Tests for paper requote schema verifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_verify_paper_schema_ok_stale_catching_up(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    bad = tmp_path / "bad.jsonl"
    good.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "requote",
                    "regime": "QUIET",
                    "fv": 0.4,
                    "cancel": 0,
                    "place": 1,
                    "flowz": 0.0,
                    "vol_ratio": 1.0,
                    "timestamp": f"2026-07-22T15:00:0{i}Z",
                }
            )
            for i in range(5)
        )
        + "\n"
    )
    bad.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "requote",
                    "regime": "QUIET",
                    "fv": 0.4,
                    "cancel": 0,
                    "place": 1,
                    "flowz": 0.0,
                }
            )
            for i in range(5)
        )
        + "\n"
    )
    ok = subprocess.run(
        [sys.executable, "scripts/verify_paper_schema.py", "--log", str(good), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert "status=OK" in ok.stderr

    stale = subprocess.run(
        [sys.executable, "scripts/verify_paper_schema.py", "--log", str(bad), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 1
    assert "status=STALE_SCHEMA" in stale.stderr
    assert "vol_ratio" in stale.stderr

    mixed = tmp_path / "mixed.jsonl"
    rows = [
        {
            "event": "requote",
            "regime": "QUIET",
            "fv": 0.4,
            "cancel": 0,
            "place": 1,
            "flowz": 0.0,
            **({"vol_ratio": 1.1} if i >= 3 else {}),
        }
        for i in range(5)
    ]
    mixed.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    catch = subprocess.run(
        [sys.executable, "scripts/verify_paper_schema.py", "--log", str(mixed), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert catch.returncode == 0
    assert "status=CATCHING_UP" in catch.stderr
