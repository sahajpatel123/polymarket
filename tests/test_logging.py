"""T1-04 structured / rotated / greppable logging."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from polymaker.loggrep import grep_logs
from polymaker.logging import configure, get_logger, required_json_fields


def test_configure_writes_rotated_json_with_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    configure(level="INFO", json_file=path, console=False)
    log = get_logger("test.logging")
    log.info("requote", condition_id="0xdeadbeefcafe", cid="0xdeadbe", fv=0.5)
    # flush handlers
    for h in logging.getLogger().handlers:
        h.flush()

    assert path.exists()
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, TimedRotatingFileHandler) for h in handlers)

    line = path.read_text().strip().splitlines()[-1]
    obj = json.loads(line)
    for field in required_json_fields():
        assert field in obj, field
    assert obj["event"] == "requote"
    assert obj["condition_id"] == "0xdeadbeefcafe"
    assert "timestamp" in obj


def test_grep_logs_by_market_and_time(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    t0 = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc).timestamp()
    rows = [
        {"timestamp": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
         "level": "info", "logger": "engine", "event": "requote",
         "condition_id": "0xaaa", "cid": "0xaaa"},
        {"timestamp": datetime.fromtimestamp(t0 + 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
         "level": "info", "logger": "engine", "event": "requote",
         "condition_id": "0xbbb", "cid": "0xbbb"},
        {"timestamp": datetime.fromtimestamp(t0 + 7200, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
         "level": "info", "logger": "engine", "event": "requote",
         "condition_id": "0xaaa", "cid": "0xaaa"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    all_a = grep_logs(path, condition_id="0xaaa")
    assert len(all_a) == 2
    window = grep_logs(path, condition_id="0xaaa", since=t0 + 1000, until=t0 + 8000)
    assert len(window) == 1
    assert window[0]["condition_id"] == "0xaaa"
