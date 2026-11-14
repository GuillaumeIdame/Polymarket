import json
from datetime import datetime, timezone

import polars as pl

from pmkt import book, fees, holders, recorder


def _h(rows):
    return pl.DataFrame([{"condition_id": "c", "token_id": t, "outcome_index": o, "proxy_wallet": w,
                          "amount": a, "avg_price": p, "unrealized_pnl": 0.0, "pseudonym": w} for t, o, w, a, p in rows])


def test_concentration_excludes_unknown_entry():
    c = holders.concentration(_h([("y", 0, "a", 60, 0.3), ("y", 0, "b", 40, 0.0)])).row(0, named=True)
    assert abs(c["top1_share"] - 0.6) < 1e-12 and abs(c["hhi"] - 5200) < 1e-9
    assert c["avg_entry_w"] == 0.3 and c["n_entry_unknown"] == 1


def test_moves_new_exit_flip():
    prev = _h([("y", 0, "a", 5000, 0.3), ("y", 0, "b", 5000, 0.4)])
    cur = _h([("n", 1, "a", 6000, 0.6), ("y", 0, "b", 9000, 0.45), ("y", 0, "c", 3000, 0.5)])
    m = holders.moves(prev, cur)
    k = {(r["proxy_wallet"], r["kind"]): r["flip"] for r in m.iter_rows(named=True)}
    assert k[("a", "EXIT")] and k[("a", "NEW")] and ("b", "ADD") in k and ("c", "NEW") in k


def test_fee_formula_matches_onchain():
    assert fees.taker_fee(32.258065, 0.62, 0.05) == 0.38  # live tx, sports
    assert abs(fees.taker_fee(10.494381, 0.89, 0.07) - 0.07191) < 1e-5  # live tx, crypto


def test_book_reconstruction_and_parse():
    t = datetime(2026, 11, 14, tzinfo=timezone.utc)
    snap = {"event_type": "book", "asset_id": "A", "market": "m", "timestamp": "1",
            "bids": [{"price": "0.40", "size": "100"}], "asks": [{"price": "0.45", "size": "50"}]}
    pc = {"event_type": "price_change", "market": "m", "timestamp": "2",
          "price_changes": [{"asset_id": "A", "price": "0.41", "size": "10", "side": "BUY"},
                            {"asset_id": "A", "price": "0.45", "size": "0", "side": "SELL"},
                            {"asset_id": "A", "price": "0.46", "size": "20", "side": "SELL"}]}
    rows = recorder.parse_message(snap, t, "c") + recorder.parse_message(pc, t.replace(second=1), "c")
    assert len(rows) == 4 and json.loads(rows[0]["bids"])[0]["price"] == "0.40"
    b = book.reconstruct(pl.DataFrame(rows, schema=recorder.WS_SCHEMA), "A")
    assert b.best_bid == 0.41 and b.best_ask == 0.46 and abs(b.mid - 0.435) < 1e-12


def test_universe_reserves_tagged_slots():
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    base = {"active": True, "closed": False, "accepting_orders": True, "asset_ids": ["x", "y"],
            "end_date": "2026-12-31T00:00:00Z"}
    m = pl.DataFrame([{**base, "market_id": str(i), "volume_24h": 1e6 - i, "tags": ["sports"]} for i in range(10)]
                     + [{**base, "market_id": "cpi", "volume_24h": 2000.0, "tags": ["cpi"]}])
    cfg = {"max_markets": 5, "include_tag_slugs": ["cpi"], "tagged_share": 0.4, "tagged_min_volume_24h": 1000,
           "min_volume_24h": 50000, "resolving_within_hours": 72}
    u = recorder.select_universe(m, cfg, now)
    assert u.height == 5 and "cpi" in u["market_id"].to_list()
