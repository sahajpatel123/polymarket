"""Tests for metrics quote schema verifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_verify_metrics_schema_ok_and_stale(tmp_path: Path) -> None:
    t0 = 1_700_000_000.0
    good = tmp_path / "good.jsonl"
    bad = tmp_path / "bad.jsonl"
    good.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": t0 + i,
                    "event": "quote",
                    "token_id": "yes",
                    "side": "BUY",
                    "price": 0.4,
                    "order_id": f"p{i}",
                    "mid": 0.41,
                    "fv_yes": 0.41,
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
                    "ts": t0 + i,
                    "event": "quote",
                    "token_id": "yes",
                    "side": "BUY",
                    "price": 0.4,
                    "order_id": f"p{i}",
                    "mid": 0.41,
                }
            )
            for i in range(5)
        )
        + "\n"
    )
    ok = subprocess.run(
        [sys.executable, "scripts/verify_metrics_schema.py", "--log", str(good), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert "status=OK" in ok.stderr

    stale = subprocess.run(
        [sys.executable, "scripts/verify_metrics_schema.py", "--log", str(bad), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 1
    assert "status=STALE_SCHEMA" in stale.stderr
    assert "fv_yes" in stale.stderr

    mixed = tmp_path / "mixed.jsonl"
    rows = [
        {
            "ts": t0 + i,
            "event": "quote",
            "token_id": "yes",
            "side": "BUY",
            "price": 0.4,
            "order_id": f"p{i}",
            "mid": 0.41,
            **({"fv_yes": 0.41} if i >= 3 else {}),
        }
        for i in range(5)
    ]
    mixed.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    catch = subprocess.run(
        [sys.executable, "scripts/verify_metrics_schema.py", "--log", str(mixed), "--tail", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert catch.returncode == 0
    assert "status=CATCHING_UP" in catch.stderr
