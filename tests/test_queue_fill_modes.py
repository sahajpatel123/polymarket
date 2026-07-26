"""Optimistic vs conservative/base fill models on the same fixture."""

from __future__ import annotations

from polymaker.domain import OpenOrder, Side
from polymaker.paper.fill_sim import FillSimulator
from polymaker.paper.queue_fill_sim import FillMode, QueueAwareFillSimulator, make_fill_simulator


def _o(oid: str, px: float = 0.50, sz: float = 100.0) -> OpenOrder:
    return OpenOrder(oid, "tok", Side.BUY, px, sz)


def test_optimistic_fills_more_than_conservative() -> None:
    opt = make_fill_simulator(FillMode.OPTIMISTIC)
    cons = make_fill_simulator(FillMode.CONSERVATIVE, default_queue_ahead=80.0)
    assert isinstance(opt, FillSimulator)
    assert isinstance(cons, QueueAwareFillSimulator)

    opt.place(_o("a"))
    cons.place(_o("a"), ts=0.0, queue_ahead=80.0)

    # Trade of 50 shares: optimistic fills 50; conservative queue eats it
    f_opt = opt.match("tok", Side.SELL, 0.49, 50.0, ts=1.0)
    f_cons = cons.match("tok", Side.SELL, 0.49, 50.0, ts=1.0)
    assert sum(f.size for f in f_opt) == 50.0
    assert sum(f.size for f in f_cons) == 0.0  # all absorbed by queue ahead
    assert cons.n_queue_blocked >= 1


def test_base_fills_after_queue_eaten() -> None:
    base = QueueAwareFillSimulator(mode=FillMode.BASE, default_queue_ahead=30.0)
    base.place(_o("b", sz=100.0), ts=0.0, queue_ahead=30.0)
    # First trade 30: only queue
    f1 = base.match("tok", Side.SELL, 0.49, 30.0, ts=1.0)
    assert sum(f.size for f in f1) == 0.0
    # Second trade 40: fills us
    f2 = base.match("tok", Side.SELL, 0.49, 40.0, ts=2.0)
    assert sum(f.size for f in f2) == 40.0
    assert base.remaining("b") == 60.0


def test_cancelled_never_fills_queue_aware() -> None:
    sim = QueueAwareFillSimulator(mode=FillMode.BASE, default_queue_ahead=0.0)
    sim.place(_o("c"), ts=0.0)
    sim.cancel("c")
    assert sim.match("tok", Side.SELL, 0.49, 100.0, ts=1.0) == []


def test_optimistic_ge_base_ge_conservative_fill_volume() -> None:
    """Same orders + trade: optimistic fill vol ≥ base ≥ conservative."""
    modes = {}
    for mode, ahead in [
        (FillMode.OPTIMISTIC, 0.0),
        (FillMode.BASE, 40.0),
        (FillMode.CONSERVATIVE, 100.0),
    ]:
        sim = make_fill_simulator(mode, default_queue_ahead=ahead)
        sim.place(_o("x", sz=100.0), ts=0.0)
        fills = sim.match("tok", Side.SELL, 0.49, 100.0, ts=10.0)
        modes[mode] = sum(f.size for f in fills)
    assert modes[FillMode.OPTIMISTIC] >= modes[FillMode.BASE]
    assert modes[FillMode.BASE] >= modes[FillMode.CONSERVATIVE]


def test_conservative_skips_equal_price_join_touch() -> None:
    """Join-BB fills are typically equal-price; conservative skips those."""
    base = QueueAwareFillSimulator(mode=FillMode.BASE, default_queue_ahead=0.0)
    cons = QueueAwareFillSimulator(mode=FillMode.CONSERVATIVE, default_queue_ahead=0.0)
    base.place(_o("j", px=0.50, sz=100.0), ts=0.0)
    cons.place(_o("j", px=0.50, sz=100.0), ts=0.0)
    # Sell aggressor at our bid (join-touch)
    f_base = base.match("tok", Side.SELL, 0.50, 40.0, ts=1.0)
    f_cons = cons.match("tok", Side.SELL, 0.50, 40.0, ts=1.0)
    assert sum(f.size for f in f_base) == 40.0
    assert sum(f.size for f in f_cons) == 0.0
    assert cons.n_queue_blocked >= 1
    # Through-price still fills under conservative with zero queue
    cons2 = QueueAwareFillSimulator(mode=FillMode.CONSERVATIVE, default_queue_ahead=0.0)
    cons2.place(_o("k", px=0.50, sz=100.0), ts=0.0)
    f_thru = cons2.match("tok", Side.SELL, 0.49, 40.0, ts=1.0)
    assert sum(f.size for f in f_thru) == 40.0
