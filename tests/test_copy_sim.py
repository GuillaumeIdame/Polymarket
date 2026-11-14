"""Copy simulator sanity: all-winners grows, all-losers shrinks, costs bite, capital lock-up is respected."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import test_kb_hypotheses as kb  # noqa: E402

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def trades(x, n=50, p=0.5, hold_h=1, gap_h=2):
    return pl.DataFrame([{"t_entry": T + timedelta(hours=gap_h * i), "t_out": T + timedelta(hours=gap_h * i + hold_h),
                          "p": p, "x": x, "category": "geopolitics"} for i in range(n)])


def test_winners_grow_losers_shrink():
    assert kb.copy_sim(trades(1.0), slip=0.0)["final"] > 400
    assert kb.copy_sim(trades(0.0), slip=0.0)["final"] < 400


def test_fair_bets_with_slippage_lose():
    alt = pl.concat([trades(1.0, 25), trades(0.0, 25).with_columns(pl.col("t_entry") + timedelta(minutes=30),
                                                                   pl.col("t_out") + timedelta(minutes=30))]).sort("t_entry")
    assert kb.copy_sim(alt, slip=0.0)["final"] > kb.copy_sim(alt, slip=0.02)["final"]


def test_capital_lockup_limits_stakes():
    long_hold = trades(1.0, n=200, hold_h=10_000, gap_h=0)   # nothing resolves inside the window
    r = kb.copy_sim(long_hold, slip=0.0, frac=0.5, max_stake=1e9)
    assert r["trades_copied"] < 200                         # cash ran out: locked until resolution
