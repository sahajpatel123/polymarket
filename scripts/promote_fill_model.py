#!/usr/bin/env python3
"""Promote the fill model using online samples collected from live/paper runs.

Merges the durable online-sample sidecar into the model's training buffer,
retrains, validates on the ONLINE slice only, and persists the artifact **only
if it passes**. Fails closed and explains why.

This is the step that converts collected evidence into a deployable model. It is
separate from the engine so promotion is an explicit, auditable act rather than a
side effect of a background retrain.

    uv run python scripts/promote_fill_model.py --model-dir models
    uv run python scripts/promote_fill_model.py --model-dir models --dry-run
"""

from __future__ import annotations

import argparse
import json
import pickle
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymaker.strategy.fill_model import (  # noqa: E402
    FillModel,
    FillTrainingStore,
)


def load_online(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as fh:
        blob = pickle.load(fh)
    return (
        np.asarray(blob["features"], dtype=np.float32),
        np.asarray(blob["y_fill"], dtype=np.float32),
        np.asarray(blob["y_markout"], dtype=np.float32),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", type=Path, default=Path("models"))
    ap.add_argument("--min-auc", type=float, default=0.55)
    ap.add_argument("--min-corr", type=float, default=0.05)
    ap.add_argument("--min-rows", type=int, default=200)
    ap.add_argument("--min-fills", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report, do not write the artifact")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    model_path = args.model_dir / "fill_model.pkl"
    online_path = args.model_dir / "fill_online_samples.pkl"
    out: dict[str, Any] = {"model": str(model_path), "online": str(online_path)}

    if not online_path.exists():
        out |= {"promoted": False, "reason": "no online samples collected"}
        print(json.dumps(out, indent=2))
        return 1
    if not model_path.exists():
        out |= {"promoted": False, "reason": "no base model artifact"}
        print(json.dumps(out, indent=2))
        return 1

    Xo, yfo, ymo = load_online(online_path)
    n_rows, n_fills = int(len(Xo)), int(yfo.sum())
    out |= {"online_rows": n_rows, "online_fills": n_fills}

    # Evidence gates first — never validate on evidence too thin to mean anything.
    if n_rows < args.min_rows or n_fills < args.min_fills:
        out |= {"promoted": False,
                "reason": f"insufficient evidence: {n_rows} rows "
                          f"(need {args.min_rows}), {n_fills} fills "
                          f"(need {args.min_fills})"}
        print(json.dumps(out, indent=2))
        return 1

    model, store = FillModel.load_bundle(model_path)
    if store is None:
        store = FillTrainingStore()
    before = store.online_arrays()
    out["online_rows_already_in_artifact"] = 0 if before is None else len(before[0])

    # Merge the online slice in, unless the artifact already carries it.
    if out["online_rows_already_in_artifact"] < n_rows:
        for i in range(n_rows):
            store.features.append(Xo[i])
            store.y_fill.append(float(yfo[i]))
            store.y_markout.append(float(ymo[i]))
            store.source.append("online")

    arrays = store.raw_arrays()
    if arrays is None:
        out |= {"promoted": False, "reason": "training buffer is empty"}
        print(json.dumps(out, indent=2))
        return 1
    X, yf, ym = arrays
    out["training_rows"] = int(len(X))

    fresh = FillModel(min_samples=100)
    fresh.train(X, yf, ym)
    if not fresh.is_trained:
        out |= {"promoted": False, "reason": "retrain did not reach trained state"}
        print(json.dumps(out, indent=2))
        return 1

    # Validate on the ONLINE slice only: the model must win on live samples, not
    # on the offline tape it was fitted to.
    online = store.online_arrays()
    assert online is not None
    metrics = fresh.validate(*online, min_auc=args.min_auc,
                             min_corr=args.min_corr)
    out["validation"] = {k: (v if not isinstance(v, np.generic) else v.item())
                         for k, v in metrics.items()}
    out["is_deployable"] = bool(fresh.is_deployable)

    if not fresh.is_deployable:
        out |= {"promoted": False,
                "reason": f"failed live validation: {metrics.get('reason')}"}
        print(json.dumps(out, indent=2, default=str))
        return 1

    if args.dry_run:
        out |= {"promoted": False, "reason": "dry run (validation passed)"}
        print(json.dumps(out, indent=2, default=str))
        return 0

    backup = model_path.with_suffix(f".pkl.bak-{int(time.time())}")
    shutil.copy2(model_path, backup)
    fresh.save(model_path, store)
    out |= {"promoted": True, "backup": str(backup),
            "reason": "passed live validation on online samples"}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
