"""Fill-model checks for the Central Bank paper engine (studies/central_bank/paper.py)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "studies" / "central_bank"))
import paper  # noqa: E402

T0 = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
META = {"tick_size": 0.01, "fee_schedule": {"rate": 0.05, "rebateRate": 0.25}, "rw_rate_per_day": None}


def book(n, bid=0.40, ask=0.42, bsz=200.0, asz=200.0):
    rows = []
    for i in range(n):
        r = {"ts": T0 + timedelta(seconds=5 * i), "bid_px_1": bid, "ask_px_1": ask, "bid_sz_1": bsz, "ask_sz_1": asz,
             "mid": (bid + ask) / 2, "microprice": (bid + ask) / 2, "tob_imb": 0.0, "top_size": (bsz + asz) / 2}
        for side in ("bid", "ask"):
            for k in range(1, 6):
                r.setdefault(f"{side}_px_{k}", None)
                r.setdefault(f"{side}_sz_{k}", None)
        rows.append(r)
    return pl.DataFrame(rows)


def tr(rows):
    return pl.DataFrame([{"ts": T0 + timedelta(seconds=s), "price_yes": p, "size": z, "taker_yes": side}
                         for s, p, z, side in rows],
                        schema={"ts": pl.Datetime("us", "UTC"), "price_yes": pl.Float64, "size": pl.Float64, "taker_yes": pl.Utf8})


def test_join_waits_for_queue():
    # quote size = 5% of 200 = 10; queue ahead at the bid = 200
    r = paper.run_outcome(book(6), tr([(7, 0.40, 150, "SELL")]), META, paper.naive_join)
    assert r["fills"] == 0
    r = paper.run_outcome(book(6), tr([(7, 0.40, 150, "SELL"), (12, 0.40, 60, "SELL")]), META, paper.naive_join)
    assert r["fills"] == 1 and abs(r["shares"] - 10) < 1e-9


def test_improve_is_front_of_queue():
    r = paper.run_outcome(book(6), tr([(7, 0.41, 3, "SELL")]), META, paper.naive_improve)
    assert r["fills"] == 1 and abs(r["shares"] - 3) < 1e-9


def test_trade_through_fills_in_full():
    r = paper.run_outcome(book(6), tr([(7, 0.38, 1, "SELL")]), META, paper.naive_join)
    assert r["fills"] == 1 and abs(r["shares"] - 10) < 1e-9


def test_reprice_through_is_picked_off():
    b = book(80)  # 400 s, so a 5-minute markout exists
    moved = pl.col("ts") >= T0 + timedelta(seconds=10)
    b = b.with_columns(pl.when(moved).then(0.39).otherwise(pl.col("ask_px_1")).alias("ask_px_1"),
                       pl.when(moved).then(0.37).otherwise(pl.col("bid_px_1")).alias("bid_px_1"),
                       pl.when(moved).then(0.38).otherwise(pl.col("mid")).alias("mid"))
    r = paper.run_outcome(b, tr([]), META, paper.naive_join)
    assert r["fills"] >= 1 and r["edge_5m"] < 0  # adverse selection shows up in the 5-minute markout
