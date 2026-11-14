"""Score wallets, test every smart-money definition walk-forward, and build the expiry screener.

Reads data/smartmoney/*.parquet (scripts/fetch_smartmoney.py) and our chain fills (maker share).
Writes data/smartmoney/{definitions,wallet_scores,screener,insider}.parquet and a JSON summary.

A definition is "validated" only if, out of sample: Spearman > 0, BH-adjusted p < 0.05, and the top quintile's
test edge after copy costs is > 0. Otherwise the screener still shows positioning, labelled as unvalidated.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import polars as pl

from pmkt import smartmoney as sm
from pmkt import storage
from pmkt.config import load_settings


def main(cutoff_days: int = 30):
    s = load_settings()
    d = s.data_dir / "smartmoney"
    meta = json.loads((d / "fetch_meta.json").read_text())
    now = datetime.fromisoformat(meta["fetched_at"])
    hist = pl.read_parquet(d / "history.parquet")
    stats = pl.read_parquet(d / "user_stats.parquet")
    hold = pl.read_parquet(d / "holders.parquet")
    mk = pl.read_parquet(d / "screener_markets.parquet")
    pos = sm.positions_table(hist)
    print(f"positions: {pos.height} resolved, {pos['wallet'].n_unique()} wallets, "
          f"{pos['event_id'].n_unique()} events, entries {pos['t_entry'].min()} .. {pos['t_entry'].max()}")

    # ---- walk-forward: score at cutoff, test on entries after it
    cutoff = now - timedelta(days=cutoff_days)
    res, panel = sm.walk_forward(pos, cutoff)
    res = res.with_columns(cutoff=pl.lit(cutoff.isoformat()))
    print(res)
    ok = res.filter((pl.col("spearman") > 0) & (pl.col("p_bh") < 0.05) & (pl.col("top_q_net") > 0)) \
        if "p_bh" in res.columns else res.clear()
    validated = ok.sort("spearman", descending=True)["definition"].to_list()
    primary = validated[0] if validated else "calibration"

    # ---- current scores (all resolved history up to now) + labels
    cur = sm.score(pos, now)
    con = storage.connect(s.data_dir)
    mm = sm.maker_share(storage.query(con, "select wallet, role from chain_fills"))
    cur = (cur.join(mm, on="wallet", how="left")
           .join(stats.select("wallet", "join_date", "trades"), on="wallet", how="left")
           .with_columns(is_mm=(pl.col("maker_share").fill_null(0) > 0.8) & (pl.col("fills").fill_null(0) >= 50)))
    q80 = cur.filter(pl.col(primary).is_not_null())[primary].quantile(0.8)
    cur = cur.with_columns(smart=(pl.col(primary) >= q80) & (pl.col(primary) >= 2.0 if primary != "conviction" else
                                                              pl.col(primary) >= q80)
                           & (pl.col("events") >= 20) & ~pl.col("is_mm"))
    cur.write_parquet(d / "wallet_scores.parquet")

    # ---- screener: one row per market ending soon
    smart = cur.filter(pl.col("smart")).select("wallet", primary)
    h = hold.join(smart, left_on="proxy_wallet", right_on="wallet", how="left")
    ins = sm.insider_flags(hold, stats, now)
    side = (h.filter(pl.col(primary).is_not_null())
            .group_by("condition_id").agg(
                smart_yes_usd=pl.when(pl.col("outcome_index") == 0).then(pl.col("current_value")).otherwise(0).sum(),
                smart_no_usd=pl.when(pl.col("outcome_index") == 1).then(pl.col("current_value")).otherwise(0).sum(),
                smart_yes_n=(pl.col("outcome_index") == 0).sum(), smart_no_n=(pl.col("outcome_index") == 1).sum()))
    flags = ins.group_by("condition_id").agg(insider_flags=pl.col("insider_sig").fill_null(False).sum(),
                                             insider_usd=pl.when(pl.col("insider_sig")).then(pl.col("current_value")).otherwise(0).sum())
    conc = hold.group_by("condition_id").agg(holders=pl.len(), top_holder_usd=pl.col("current_value").max())
    end = pl.col("end_date").str.to_datetime(strict=False, time_zone="UTC")
    scr = (mk.with_columns(end_ts=end,
                           mid=pl.when(pl.col("best_bid").is_not_null() & pl.col("best_ask").is_not_null())
                           .then((pl.col("best_bid") + pl.col("best_ask")) / 2).otherwise(pl.col("last_trade_price")))
           .with_columns(hours_left=(pl.col("end_ts") - pl.lit(now)).dt.total_seconds() / 3600,
                         yes_label=pl.col("outcomes").list.get(0, null_on_oob=True),
                         no_label=pl.col("outcomes").list.get(1, null_on_oob=True))
           .join(side, on="condition_id", how="left").join(flags, on="condition_id", how="left")
           .join(conc, on="condition_id", how="left")
           .with_columns(pl.col("smart_yes_usd", "smart_no_usd", "insider_usd").fill_null(0.0),
                         pl.col("smart_yes_n", "smart_no_n", "insider_flags").fill_null(0))
           .with_columns(smart_net_usd=pl.col("smart_yes_usd") - pl.col("smart_no_usd")))
    lean = (pl.when((pl.col("smart_yes_n") + pl.col("smart_no_n")) < 2).then(pl.lit(None))
            .when(pl.col("smart_net_usd") > 0.6 * (pl.col("smart_yes_usd") + pl.col("smart_no_usd"))).then(pl.col("yes_label"))
            .when(pl.col("smart_net_usd") < -0.6 * (pl.col("smart_yes_usd") + pl.col("smart_no_usd"))).then(pl.col("no_label"))
            .otherwise(pl.lit("split")))
    scr = scr.with_columns(smart_lean=lean,
                           near_res=(pl.col("mid") >= 0.95) | (pl.col("mid") <= 0.05),
                           spread_c=100 * (pl.col("best_ask") - pl.col("best_bid")))
    scr = scr.select("market_id", "condition_id", "question", "category", "slug", "event_slug", "end_ts", "hours_left",
                     "yes_label", "no_label", "best_bid", "best_ask", "mid", "spread_c", "volume_24h", "liquidity",
                     "fee_rate", "uma_resolution_status", "near_res", "holders", "top_holder_usd",
                     "smart_yes_n", "smart_no_n", "smart_yes_usd", "smart_no_usd", "smart_net_usd", "smart_lean",
                     "insider_flags", "insider_usd").sort("volume_24h", descending=True, nulls_last=True)
    scr.write_parquet(d / "screener.parquet")
    ins.filter(pl.col("insider_sig").fill_null(False)).join(
        mk.select("condition_id", "question"), on="condition_id", how="left").write_parquet(d / "insider.parquet")
    res.write_parquet(d / "definitions.parquet")
    summary = {"primary": primary, "validated": validated, "cutoff": cutoff.isoformat(), "as_of": now.isoformat(),
               "wallets_scored": cur.height, "smart_wallets": int(cur["smart"].sum()),
               "screener_markets": scr.height, "positions": pos.height, "events": pos["event_id"].n_unique()}
    (d / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
