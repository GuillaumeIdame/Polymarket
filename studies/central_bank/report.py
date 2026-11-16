"""Weekly report data for the dashboard: writes art/*.json ({rows, meta}) from the recorder lake and out/*.json.

Run after signals.py and paper.py:  python3 signals.py && python3 paper.py && python3 report.py
"""
from __future__ import annotations

import json

import polars as pl
from cb_common import DATA_DIR, HERE, OUT_DIR, connect, load_markets
from cb_data import books, trades

ART = HERE / "art"


def r4(x):
    return round(x, 5) if isinstance(x, float) else x


def code_meta(names):
    return [{"label": n, "value": (HERE / n).read_text(), "type": "code", "lang": "python"} for n in names]


def main() -> None:
    ART.mkdir(exist_ok=True)
    b, t = books(), trades()
    mk = {(m["meeting"], m["outcome"]): m for m in load_markets()}
    con = connect(DATA_DIR)
    sess = con.sql("SELECT * FROM cb_sessions ORDER BY ts").pl()
    first, last = b["ts"].min(), b["ts"].max()
    hours = (last - first).total_seconds() / 3600
    expected = int(hours * 3600 / 5) + 1
    src = [{"label": "Window", "value": f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC ({hours:.1f} h)"},
           {"label": "Source", "value": "Polymarket CLOB /books every 5 s + market websocket trades (public)"}]

    # coverage and book stats per outcome
    agg = (b.group_by(["meeting", "outcome"]).agg(
        snapshots=pl.len(), last_mid=pl.col("mid").drop_nulls().last(), spread_c=(pl.col("spread").mean() * 100),
        top_size=pl.col("top_size").mean(), bid_depth5=pl.sum_horizontal([pl.col(f"bid_sz_{i}").fill_null(0) for i in range(1, 6)]).mean(),
        ask_depth5=pl.sum_horizontal([pl.col(f"ask_sz_{i}").fill_null(0) for i in range(1, 6)]).mean(),
        one_sided=(pl.col("mid").is_null().mean())).sort(["meeting", "outcome"]))
    tcount = (t.group_by(["meeting", "outcome"]).agg(trades=pl.len(), trade_shares=pl.col("size").sum())
              if t.height else pl.DataFrame(schema={"meeting": pl.Utf8, "outcome": pl.Utf8, "trades": pl.UInt32, "trade_shares": pl.Float64}))
    agg = agg.join(tcount, on=["meeting", "outcome"], how="left").with_columns(pl.col("trades").fill_null(0), pl.col("trade_shares").fill_null(0))
    stats_rows = []
    for r in agg.to_dicts():
        m = mk[(r["meeting"], r["outcome"])]
        stats_rows.append({**{k: r4(v) for k, v in r.items()}, "key": f"{r['meeting']}|{r['outcome']}", "bp": m["bp"],
                           "tick_c": m["tick_size"] * 100, "reward_per_day": m.get("rw_rate_per_day"),
                           "reward_max_spread_c": m.get("rw_rewards_max_spread"), "reward_min_size": m.get("rw_rewards_min_size"),
                           "quote_size": max(0, int(0.05 * (r["top_size"] or 0))), "url": m["event_url"],
                           "coverage": r["snapshots"] / expected})

    # 1-minute mids for charts
    mids = (b.with_columns(minute=pl.col("ts").dt.truncate("1m")).group_by(["meeting", "outcome", "minute"])
            .agg(mid=pl.col("mid").drop_nulls().last()).drop_nulls("mid").sort(["meeting", "outcome", "minute"]))
    mid_rows = [{"meeting": r["meeting"], "outcome": r["outcome"], "ts": r["minute"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "mid": r4(r["mid"])} for r in mids.to_dicts()]

    sig = json.loads((OUT_DIR / "signals.json").read_text())
    sig_rows = [{**{k: r4(v) for k, v in r.items()}, "key": f"{r['meeting']}|{r['split']}|{r['signal']}|{r['horizon']}"}
                for r in sig["rows"]]
    curve = json.loads((OUT_DIR / "curve.json").read_text())
    paper = json.loads((OUT_DIR / "paper.json").read_text())
    paper_rows = [{**{k: r4(v) for k, v in r.items()}, "key": f"{r['strategy']}|{r['meeting']}"} for r in paper["summary"]]

    gaps = sess.filter(pl.col("kind").is_in(["gap_books", "error_books", "ws_disconnect"]))
    run_rows = [{
        "window_start": f"{first:%Y-%m-%d %H:%M} UTC", "window_end": f"{last:%Y-%m-%d %H:%M} UTC", "hours": round(hours, 2),
        "snapshots": b.height, "expected_snapshots": expected * len(mk), "trades": t.height,
        "outcomes": len(mk), "meetings": len({k[0] for k in mk}), "gaps": gaps.filter(pl.col("kind") == "gap_books").height,
        "book_errors": gaps.filter(pl.col("kind") == "error_books").height,
        "reconnects": max(0, sess.filter(pl.col("kind") == "ws_connect").height - sess.filter(pl.col("kind") == "start").height),
        "spoof_events": sig["spoof_events"], "anchor_status": "missing: no ZQ futures source supplied",
        "news_status": "missing: no data calendar supplied", "kalshi_status": "blocked: network proxy refused Kalshi API",
        "strategies_status": "placeholders: pm_mm_backtest.py not supplied",
        "phase2_status": "preliminary (prompt asks for 1 week of data first)",
    }]
    sess_rows = [{"ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S"), "kind": r["kind"], "detail": r["detail"],
                  "gap_s": r4(r["gap_s"])} for r in sess.to_dicts()]

    files = {
        "run": (run_rows, code_meta(["recorder.py", "cb_common.py"])),
        "book_stats": (stats_rows, []), "mids": (mid_rows, []),
        "signals": (sig_rows, code_meta(["signals.py", "cb_data.py"]) + [{"label": "Note", "value":
                    "Hit rate = share of non-zero mid moves whose sign matches the signal. n_indep ~ snapshots x 5 s / horizon (overlapping windows)."}]),
        "curve": (curve, [{"label": "Note", "value": "Expected move = sum(bp x mid) / sum(mid) per meeting; 50+ bp buckets valued at 50 bp. ZQ strip missing, so the gap is null."}]),
        "paper": (paper_rows, code_meta(["paper.py"]) + [{"label": "Note", "value":
                  "Placeholder strategies P1-P5 until pm_mm_backtest.py is supplied. Rewards are a rough estimate."}]),
        "sessions": (sess_rows, []),
    }
    for name, (rows, extra) in files.items():
        (ART / f"{name}.json").write_text(json.dumps({"rows": rows, "meta": src + extra}, separators=(",", ":"), default=str)
                                          .replace("NaN", "null"))
    # backtester-schema CSV (bid, ask, bid_size, ask_size, trade_side, trade_size, anchor, news), one file per outcome
    csv_dir = DATA_DIR / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    tt = t.with_columns(ts5=pl.col("ts").dt.truncate("5s")) if t.height else None
    for (mt, oc), g in b.group_by(["meeting", "outcome"]):
        g = g.with_columns(ts5=pl.col("ts").dt.truncate("5s"))
        if tt is not None:
            agg_t = (tt.filter((pl.col("meeting") == mt) & (pl.col("outcome") == oc))
                     .group_by("ts5").agg(trade_size=pl.col("size").sum(),
                                          trade_side=pl.when((pl.when(pl.col("taker_yes") == "BUY").then(pl.col("size")).otherwise(-pl.col("size"))).sum() >= 0)
                                          .then(pl.lit("BUY")).otherwise(pl.lit("SELL"))))
            g = g.join(agg_t, on="ts5", how="left")
        else:
            g = g.with_columns(trade_size=pl.lit(None, pl.Float64), trade_side=pl.lit(None, pl.Utf8))
        g.select(pl.col("ts"), pl.col("bid_px_1").alias("bid"), pl.col("ask_px_1").alias("ask"),
                 pl.col("bid_sz_1").alias("bid_size"), pl.col("ask_sz_1").alias("ask_size"), "trade_side", "trade_size",
                 "anchor", "news").sort("ts").write_csv(csv_dir / f"{mt}_{oc.replace(' ', '_').replace('+', 'plus')}.csv")
    print(f"art: {', '.join(f'{k}={len(v[0])}' for k, v in files.items())}; csv -> {csv_dir}")


if __name__ == "__main__":
    main()
