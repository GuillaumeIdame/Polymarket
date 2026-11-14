"""Unbiased trade-level history for the smart-money model.

Why: /v2/positions history is winner-biased (losing positions on inactive markets disappear, winners stay as
CLOSED). Measured 2026-11-14: +6.7c to +11.6c excess win per share across 2.4M positions -> unusable for skill.
/v2/activity lists every TRADE regardless of outcome, and /v2/resolutions gives exact payouts.

Universe (activity-selected, never performance-selected):
  half = random sample of current holders in markets ending soon (data/smartmoney/holders.parquet),
  half = random sample of wallets with >= 5 on-chain fills in our lake.
Window: last `days` days of TRADE activity, at most `max_pages` x 1000 rows per wallet (flagged if capped).
Output: data/smartmoney/{trades,resolutions}.parquet, activity_meta.json
"""
from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import polars as pl

from pmkt import storage
from pmkt.config import load_settings
from pmkt.http import Http

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from fetch_smartmoney import Pace  # noqa: E402

API = "https://data-api.polymarket.com/v2"


def main(n_wallets: int = 1600, days: int = 60, max_pages: int = 5):
    s = load_settings()
    d = s.data_dir / "smartmoney"
    http, pace = Http(min_interval_s=0.0), Pace(16)

    def get(url, params):
        pace.wait()
        return http.get(url, params)

    rnd = random.Random(11)
    excl = {a.lower() for a in s["chain"]["exclude_from_holders"]}
    holders = sorted(set(pl.read_parquet(d / "holders.parquet")["proxy_wallet"].to_list()) - excl)
    con = storage.connect(s.data_dir)
    fills = storage.query(con, "select wallet from chain_fills group by 1 having count(*) >= 5")["wallet"].to_list()
    fills = sorted(set(fills) - excl - set(holders))
    wallets = rnd.sample(holders, min(n_wallets // 2, len(holders))) + rnd.sample(fills, min(n_wallets // 2, len(fills)))
    origin = {w: "holder" for w in wallets[: n_wallets // 2]} | {w: "fills" for w in wallets[n_wallets // 2:]}
    now = datetime.now(timezone.utc)
    start = int((now - timedelta(days=days)).timestamp())
    keep = ("timestamp", "condition_id", "token_id", "outcome_index", "side", "price", "size", "usdc_size",
            "transaction_hash", "title", "slug", "event_slug")

    def acts(w):
        rows, capped = [], False
        params = {"user": w, "type": "TRADE", "limit": 1000, "start": start, "sort_direction": "DESC"}
        try:
            for i in range(max_pages):
                body = get(f"{API}/activity", params)
                rows += [{"wallet": w, **{k: r.get(k) for k in keep}} for r in body.get("data") or []]
                cur = (body.get("pagination") or {}).get("next_cursor")
                if not cur:
                    break
                params["cursor"] = cur
                capped = i == max_pages - 1
        except Exception as e:  # noqa: BLE001
            return rows, {"wallet": w, "error": str(e)[:200]}
        return rows, {"wallet": w, "origin": origin[w], "trades": len(rows), "capped": capped}

    t0, trades, wmeta = time.time(), [], []
    with ThreadPoolExecutor(10) as ex:
        for i, (r, m) in enumerate(ex.map(acts, wallets), 1):
            trades += r
            wmeta.append(m)
            if i % 400 == 0:
                print(f"  {i}/{len(wallets)} wallets, {len(trades)} trades, {time.time() - t0:.0f}s", flush=True)
    tr = pl.DataFrame(trades, infer_schema_length=None)
    tr.write_parquet(d / "trades.parquet")
    pl.DataFrame(wmeta, infer_schema_length=None).write_parquet(d / "activity_wallets.parquet")

    cids = sorted(set(tr["condition_id"].drop_nulls().to_list()))
    print(f"trades: {tr.height}; resolving {len(cids)} conditions", flush=True)

    def res(chunk):
        try:
            return get(f"{API}/resolutions", {"condition": ",".join(chunk)}).get("data") or []
        except Exception:  # noqa: BLE001
            return []
    with ThreadPoolExecutor(8) as ex:
        rs = [x for part in ex.map(res, [cids[i:i + 20] for i in range(0, len(cids), 20)]) for x in part]
    rows = [{"condition_id": r["condition_id"], "status": r.get("status"), "payouts": r.get("payouts"),
             "resolved_at": r.get("resolved_at"), "was_disputed": r.get("was_disputed")} for r in rs]
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(d / "resolutions.parquet")
    (d / "activity_meta.json").write_text(json.dumps({"fetched_at": now.isoformat(), "days": days,
                                                      "wallets": len(wallets), "trades": tr.height,
                                                      "conditions": len(cids), "resolutions": len(rows)}))
    print(f"done in {time.time() - t0:.0f}s: {tr.height} trades, {len(rows)} resolutions", flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(int(a[0]) if a else 1600, int(a[1]) if len(a) > 1 else 60)


def backfill_null_payouts(data_dir=None, per_call: int = 50) -> int:
    """/v2/resolutions returns status=resolved with payouts=null for many neg-risk / multi-outcome markets
    (weather, tweet counts, price ladders): 45k of 208k resolved conditions on 2026-11-14. Fill payouts from
    Gamma's final outcomePrices (closed=true, filtered by condition_ids), scaled to ppm like /v2/resolutions."""
    s = load_settings()
    d = (data_dir or s.data_dir) / "smartmoney"
    r = pl.read_parquet(d / "resolutions.parquet")
    need = r.filter((pl.col("status") == "resolved") & pl.col("payouts").is_null())["condition_id"].to_list()
    http, pace = Http(min_interval_s=0.0), Pace(14)

    def one(chunk):
        pace.wait()
        try:
            ms = http.get(f"{s.endpoint('gamma')}/markets",
                          [("condition_ids", c) for c in chunk] + [("closed", "true"), ("limit", per_call)])
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for m in ms:
            try:
                px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except (TypeError, ValueError):
                continue
            if len(px) == 2 and all(p in (0.0, 0.5, 1.0) for p in px):   # only clean final prices
                out[m["conditionId"]] = [int(round(p * 1e6)) for p in px]
        return out
    with ThreadPoolExecutor(6) as ex:
        filled = {k: v for part in ex.map(one, [need[i:i + per_call] for i in range(0, len(need), per_call)])
                  for k, v in part.items()}
    fill = pl.DataFrame({"condition_id": list(filled), "payouts_fill": list(filled.values())},
                        schema={"condition_id": pl.Utf8, "payouts_fill": pl.List(pl.Int64)})
    r = r.join(fill, on="condition_id", how="left").with_columns(
        payouts=pl.coalesce("payouts", "payouts_fill"),
        payout_source=pl.when(pl.col("payouts_fill").is_not_null()).then(pl.lit("gamma_final_price")).otherwise(pl.lit("resolutions")))
    r.drop("payouts_fill").write_parquet(d / "resolutions.parquet")
    print(f"backfilled {len(filled)} of {len(need)} null-payout conditions from Gamma")
    return len(filled)
