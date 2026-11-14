"""Fetch the inputs for the screener and the smart-money model.

1. Screener universe: open markets ending in the next N hours (Gamma, ordered by 24h volume).
2. Holders of those markets (Data API v2 /holders with entry & PnL, top 100 per outcome).
3. Wallet universe = those holders (position >= min_value) + active wallets from our on-chain fills.
   Selected on current activity only, never on past performance (no hindsight selection).
4. Per wallet: resolved history = CLOSED positions + REDEEMABLE positions (incl. archived).
   CLOSED alone is biased toward winners: losing positions are usually never redeemed and stay REDEEMABLE.
5. Per wallet: /v2/user-stats (join date, trade count) for insider-signature features.

Rate limits (docs, 2026-11-14): /v2 general 800 req/10s, /v2/positions 200 req/10s. We stay near 15 req/s.
Usage: python scripts/fetch_smartmoney.py [hours=72] [max_wallets=2500]
Output: data/smartmoney/{screener_markets,holders,history,user_stats}.parquet
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import polars as pl

from pmkt import gamma, storage
from pmkt.config import load_settings
from pmkt.http import Http

API = "https://data-api.polymarket.com/v2"


class Pace:
    """Global request pacer shared by all threads."""

    def __init__(self, per_s: float):
        self.dt, self.lock, self.next = 1.0 / per_s, threading.Lock(), time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.dt
        time.sleep(max(0.0, t - now))


def main(hours: float = 72, max_wallets: int = 2500, min_value: float = 200.0):
    s = load_settings()
    out = s.data_dir / "smartmoney"
    out.mkdir(parents=True, exist_ok=True)
    http = Http(min_interval_s=0.0)
    pace = Pace(14)

    def get(url, params):
        pace.wait()
        return http.get(url, params)

    # 1) screener universe
    now = datetime.now(timezone.utc)
    rows, seen = [], set()
    for off in range(0, 1500, 100):
        ms = get(f"{s.endpoint('gamma')}/markets", {
            "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100, "offset": off,
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        for m in ms:
            if m.get("id") in seen or not m.get("acceptingOrders"):
                continue
            seen.add(m.get("id"))
            r = gamma.flatten_market((m.get("events") or [{}])[0], m, now)
            r.pop("raw_json"), r.pop("description")
            rows.append(r)
        if len(ms) < 100:
            break
    mk = pl.DataFrame(rows, infer_schema_length=None)
    # categories: /markets embeds events without tags -> batch lookup
    evs = sorted({e for e in mk["event_id"].to_list() if e and e != "None"})
    tagmap = {}
    for i in range(0, len(evs), 50):
        for ev in get(f"{s.endpoint('gamma')}/events", [("id", e) for e in evs[i:i + 50]] + [("limit", 100)]):
            tagmap[str(ev["id"])] = [t.get("slug") for t in ev.get("tags") or [] if t.get("slug")]
    mk = mk.with_columns(tags=pl.col("event_id").replace_strict(tagmap, default=[], return_dtype=pl.List(pl.Utf8)))
    mk = mk.with_columns(category=pl.col("tags").map_elements(gamma.category_from_tags, return_dtype=pl.Utf8))
    mk.write_parquet(out / "screener_markets.parquet")
    print(f"screener markets ending within {hours}h: {mk.height}", flush=True)

    # 2) holders (top 100 per outcome, with entry/PnL)
    def holders(cid):
        try:
            body = get(f"{API}/holders", {"condition": cid, "limit": 100, "include_pnl": "true"})
        except Exception as e:  # noqa: BLE001
            return [{"condition_id": cid, "error": str(e)[:200]}]
        return [{"condition_id": cid, **h} for t in body.get("data") or [] for h in t.get("holders") or []]
    with ThreadPoolExecutor(8) as ex:
        hrows = [r for rs in ex.map(holders, mk["condition_id"].to_list()) for r in rs]
    hd = pl.DataFrame(hrows, infer_schema_length=None)
    if "error" in hd.columns:
        print("holder errors:", hd.filter(pl.col("error").is_not_null()).height, flush=True)
        hd = hd.filter(pl.col("error").is_null()).drop("error")
    hd = hd.with_columns(pl.col("proxy_wallet").str.to_lowercase())
    hd.write_parquet(out / "holders.parquet")
    print(f"holder rows: {hd.height}", flush=True)

    # 3) wallet universe (activity-based, no performance filter)
    w_hold = (hd.filter(pl.col("current_value") >= min_value).group_by("proxy_wallet")
              .agg(v=pl.col("current_value").sum()).sort("v", descending=True)["proxy_wallet"].to_list())
    con = storage.connect(s.data_dir)
    excl = {a.lower() for a in s["chain"]["exclude_from_holders"]}
    w_fill = storage.query(con, "select wallet, count(*) n from chain_fills group by 1 having n >= 20 order by n desc")
    w_fill = [w for w in w_fill["wallet"].to_list() if w not in excl]
    wallets, sw = [], set()
    for w in w_hold + w_fill:   # holders of screener markets first: they drive the positioning signal
        if w not in sw and w not in excl:
            sw.add(w)
            wallets.append(w)
        if len(wallets) >= max_wallets:
            break
    print(f"wallets: {len(wallets)} ({sum(w in set(w_hold) for w in wallets)} screener holders)", flush=True)

    # 4) resolved history per wallet
    keep = ("token_id", "condition_id", "event_id", "title", "slug", "outcome", "outcome_index", "avg_price",
            "total_size", "current_size", "current_price", "realized_pnl", "unrealized_pnl", "total_pnl",
            "total_cost_usdc", "status", "first_entry_at", "last_event_at", "end_date", "negative_risk")

    def history(w):
        res, pages = [], {"CLOSED": 6, "REDEEMABLE": 4}
        try:
            for st, maxp in pages.items():
                params = {"user": w, "limit": 500, "status": st}
                if st != "CLOSED":
                    params["include_archived"] = "true"   # CLOSED rejects the flag
                for _ in range(maxp):
                    body = get(f"{API}/positions", params)
                    res += [{"wallet": w, **{k: r.get(k) for k in keep}} for r in body.get("data") or []]
                    cur = (body.get("pagination") or {}).get("next_cursor")
                    if not cur:
                        break
                    params["cursor"] = cur
            us = get(f"{API}/user-stats", {"user": w}).get("data") or {}
            stats = {"wallet": w, "join_date": us.get("join_date"), "trades": us.get("trades"),
                     "all_time_realized": (us.get("all_time_pnl") or {}).get("realized_pnl")}
        except Exception as e:  # noqa: BLE001
            return res, {"wallet": w, "error": str(e)[:200]}
        return res, stats

    t0, hist, stats = time.time(), [], []
    with ThreadPoolExecutor(10) as ex:
        for i, (h, st) in enumerate(ex.map(history, wallets), 1):
            hist += h
            stats.append(st)
            if i % 250 == 0:
                print(f"  {i}/{len(wallets)} wallets, {len(hist)} positions, {time.time() - t0:.0f}s", flush=True)
    pl.DataFrame(hist, infer_schema_length=None).write_parquet(out / "history.parquet")
    pl.DataFrame(stats, infer_schema_length=None).write_parquet(out / "user_stats.parquet")
    (out / "fetch_meta.json").write_text(json.dumps({"fetched_at": now.isoformat(), "hours": hours,
                                                      "wallets": len(wallets), "positions": len(hist)}))
    print(f"done: {len(hist)} positions for {len(wallets)} wallets in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(float(a[0]) if a else 72, int(a[1]) if len(a) > 1 else 2500)
