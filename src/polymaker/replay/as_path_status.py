"""Aggregate AS-path readiness gates into one status board.

Does not change strategy. Reads journals + optional prior evidence JSON
to answer: can conservative adverse-selection EV bind on this tape?
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.domain import MarketMeta
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.replay.through_price_tape import measure_through_price_tape


@dataclass(frozen=True)
class AsPathStatus:
    slug: str
    journal: str
    n_through: int
    n_at_touch: int
    conservative_join_viable: bool
    blockers: tuple[str, ...]
    ready: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "journal": self.journal,
            "n_through": self.n_through,
            "n_at_touch": self.n_at_touch,
            "conservative_join_viable": self.conservative_join_viable,
            "blockers": list(self.blockers),
            "ready": self.ready,
            "note": self.note,
        }


def assess_as_path(
    rows: list[dict[str, Any]],
    meta: MarketMeta,
    *,
    journal: str = "",
) -> AsPathStatus:
    tape = measure_through_price_tape(rows, meta)
    blockers: list[str] = []
    if tape.n_through == 0:
        blockers.append("no_through_price_sells")
    if tape.n_at_touch > 0 and tape.n_through == 0:
        blockers.append("sells_at_touch_only_equal_price_skip")
    blockers.append("join_best_bid_default_off")
    blockers.append("finding_requires_n_fill_candidate_gt_0")
    ready = tape.n_through > 0  # necessary, not sufficient
    note = (
        "ready=true only means tape has through-price sells (necessary for "
        "conservative join fills). Still need multi-market finding + promo gates."
        if ready
        else "AS path blocked: need denser through-price tape or Tier-2 equal-price policy."
    )
    return AsPathStatus(
        slug=meta.slug,
        journal=journal,
        n_through=tape.n_through,
        n_at_touch=tape.n_at_touch,
        conservative_join_viable=tape.conservative_join_viable,
        blockers=tuple(blockers),
        ready=ready,
        note=note,
    )


def assess_as_path_from_journal(
    journal: Path,
    meta: MarketMeta,
) -> AsPathStatus:
    rows = filter_rows_for_tokens(
        load_journal(journal),
        yes_token=meta.yes.token_id,
        no_token=meta.no.token_id,
    )
    return assess_as_path(rows, meta, journal=str(journal))
