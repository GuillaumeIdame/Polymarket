"""Live check: record N assets for T seconds, then compare reconstructed books vs REST /book.
Usage: python scripts/validate_book.py [seconds] [n_assets]"""
import asyncio
import os
import sys
import tempfile

import httpx
import polars as pl

from pmkt import book, gamma, recorder, storage
from pmkt.config import load_settings
from pmkt.http import Http

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 90
n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
os.environ["PMKT_DATA_DIR"] = tempfile.mkdtemp(prefix="pmkt_val_")
s = load_settings()
m = gamma.universe_candidates(Http(), s.endpoint("gamma"), s["recorder"]["universe"])
u = recorder.select_universe(m, s["recorder"]["universe"])
assets = [ids[0] for ids in u["asset_ids"].to_list()[:n]]
asyncio.run(recorder.record(s, lambda: set(assets), duration_s=secs))
con = storage.connect(s.data_dir)
ev = storage.query(con, "select * from ws_market")
c = httpx.Client(timeout=30)
rows = []
for a in assets:
    rb = c.get(f"{s.endpoint('clob')}/book", params={"token_id": a}).json()
    rbids = {float(lv_["price"]): float(lv_["size"]) for lv_ in rb.get("bids", [])}
    rasks = {float(lv_["price"]): float(lv_["size"]) for lv_ in rb.get("asks", [])}
    b = book.reconstruct(ev, a)
    lv = set(rbids) | set(b.bids) | set(rasks) | set(b.asks)
    mism = sum(1 for p in set(rbids) | set(b.bids) if abs(rbids.get(p, 0) - b.bids.get(p, 0)) > 1e-6) + \
           sum(1 for p in set(rasks) | set(b.asks) if abs(rasks.get(p, 0) - b.asks.get(p, 0)) > 1e-6)
    rows.append({"asset": a[:10], "rest_bb": max(rbids) if rbids else None, "rec_bb": b.best_bid,
                 "rest_ba": min(rasks) if rasks else None, "rec_ba": b.best_ask, "levels": len(lv),
                 "level_mismatch": mism, "n_updates": ev.filter(pl.col("asset_id") == a).height})
df = pl.DataFrame(rows)
pl.Config.set_tbl_rows(50)
print(df)
tob = df.filter((pl.col("rest_bb") == pl.col("rec_bb")) & (pl.col("rest_ba") == pl.col("rec_ba"))).height
print(f"top-of-book exact match: {tob}/{df.height}; total level mismatches: {df['level_mismatch'].sum()} "
      f"of {df['levels'].sum()} levels")
