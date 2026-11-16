"""Phase 2: do book and flow imbalance predict short-term mid changes?  (PROMPT.md §5)

Per meeting (never pooled across meetings): correlation and hit rate of each signal vs the mid change at
30 s / 1 m / 5 m / 15 m, split by news window and by outcome price bucket. Also the spoofing test
and the daily curve check. Writes out/signals.json and out/curve.json.
"""
from __future__ import annotations

import json
import math

import polars as pl
from cb_common import OUT_DIR, bp_for
from cb_data import books, trades

HORIZONS = {"30s": 30, "1m": 60, "5m": 300, "15m": 900}
SIGNALS = ["tob_imb", "l5_imb", "micro_minus_mid", "flow_imb_5m", "anchor_minus_mid"]
BUCKETS = [(0, 0.10, "<10c"), (0.10, 0.40, "10-40c"), (0.40, 0.60, "40-60c"), (0.60, 0.90, "60-90c"), (0.90, 1.01, ">90c")]


def add_flow(b: pl.DataFrame, t: pl.DataFrame) -> pl.DataFrame:
    """Trailing 5-minute taker flow imbalance (buy - sell)/(buy + sell) in YES shares."""
    if t.height == 0:
        return b.with_columns(flow_imb_5m=pl.lit(None, pl.Float64), flow_n_5m=pl.lit(0))
    t = t.with_columns(signed=pl.when(pl.col("taker_yes") == "BUY").then(pl.col("size")).otherwise(-pl.col("size")))
    out = []
    for (mt, oc), g in b.group_by(["meeting", "outcome"], maintain_order=True):
        tt = t.filter((pl.col("meeting") == mt) & (pl.col("outcome") == oc)).sort("ts")
        if tt.height == 0:
            out.append(g.with_columns(flow_imb_5m=pl.lit(None, pl.Float64), flow_n_5m=pl.lit(0)))
            continue
        roll = tt.rolling("ts", period="5m").agg(net=pl.col("signed").sum(), gross=pl.col("size").sum(), n=pl.len())
        roll = roll.unique("ts", keep="last").sort("ts")
        j = g.sort("ts").join_asof(roll, on="ts", strategy="backward", tolerance="5m")
        out.append(j.with_columns(flow_imb_5m=pl.col("net") / pl.col("gross"), flow_n_5m=pl.col("n").fill_null(0))
                   .drop(["net", "gross", "n"]))
    return pl.concat(out, how="diagonal_relaxed")


def add_targets(b: pl.DataFrame) -> pl.DataFrame:
    out = []
    for _, g in b.group_by(["meeting", "outcome"], maintain_order=True):
        g = g.sort("ts")
        for name, sec in HORIZONS.items():
            fut = g.select(pl.col("ts") - pl.duration(seconds=sec), pl.col("mid").alias(f"mid_{name}"))
            g = g.join_asof(fut.sort("ts"), on="ts", strategy="forward", tolerance="10s")
            g = g.with_columns((pl.col(f"mid_{name}") - pl.col("mid")).alias(f"d_{name}"))
        out.append(g)
    return pl.concat(out)


def stats(df: pl.DataFrame, sig: str, h: str) -> dict:
    d = df.select(pl.col(sig).alias("s"), pl.col(f"d_{h}").alias("y")).drop_nulls()
    d = d.filter(pl.col("s").is_finite())
    n = d.height
    moves = d.filter((pl.col("y") != 0) & (pl.col("s") != 0))
    corr = None
    if n > 10 and d["s"].std() and d["y"].std():
        corr = d.select(pl.corr("s", "y")).item()
    hit = None
    if moves.height:
        hit = moves.select(((pl.col("s").sign() == pl.col("y").sign()).cast(pl.Float64)).mean()).item()
    return {"n": n, "n_moves": moves.height, "corr": corr, "hit_rate": hit,
            # overlapping windows: roughly one independent observation per horizon length per outcome
            "n_indep": int(n * 5 / HORIZONS[h])}


def spoof_events(b: pl.DataFrame, t: pl.DataFrame) -> pl.DataFrame:
    """Large resting orders (>= 5x the outcome's median level size, top 5 levels) that appear and vanish within 30 s
    without a trade at that price. Returns snapshot rows (meeting, outcome, ts) right after each vanish."""
    rows = []
    for (mt, oc), g in b.group_by(["meeting", "outcome"], maintain_order=True):
        g = g.sort("ts")
        sizes = [x for i in range(1, 6) for x in g[f"bid_sz_{i}"].drop_nulls().to_list() + g[f"ask_sz_{i}"].drop_nulls().to_list()]
        if not sizes:
            continue
        big = 5 * sorted(sizes)[len(sizes) // 2]
        traded = set()
        if t.height:
            traded = {(round(p, 4)) for p in t.filter((pl.col("meeting") == mt) & (pl.col("outcome") == oc))["price_yes"].to_list()}
        prev = {}
        appeared = {}
        for r in g.iter_rows(named=True):
            cur = {}
            for side in ("bid", "ask"):
                for i in range(1, 6):
                    p, s = r[f"{side}_px_{i}"], r[f"{side}_sz_{i}"]
                    if p is not None:
                        cur[(side, round(p, 4))] = s or 0
            for k, s in cur.items():
                if s - prev.get(k, 0) >= big:
                    appeared[k] = (r["ts"], s - prev.get(k, 0))
            for k, (t0, add) in list(appeared.items()):
                age = (r["ts"] - t0).total_seconds()
                if age > 30:
                    del appeared[k]
                elif prev.get(k, 0) - cur.get(k, 0) >= 0.8 * add and k[1] not in traded:
                    rows.append({"meeting": mt, "outcome": oc, "ts": r["ts"], "price": k[1], "side": k[0], "size": add})
                    del appeared[k]
            prev = cur
    return pl.DataFrame(rows) if rows else pl.DataFrame(schema={"meeting": pl.Utf8, "outcome": pl.Utf8,
                                                                 "ts": pl.Datetime("us", "UTC"), "price": pl.Float64,
                                                                 "side": pl.Utf8, "size": pl.Float64})


def curve(b: pl.DataFrame) -> list[dict]:
    """Expected move per meeting = sum(bp_i * mid_i) / sum(mid_i); hourly, plus the 3-meeting total."""
    hb = (b.with_columns(hour=pl.col("ts").dt.truncate("1h"),
                         bp=pl.col("outcome").map_elements(bp_for, return_dtype=pl.Int64))
          .group_by(["hour", "meeting", "outcome", "bp"]).agg(mid=pl.col("mid").last()))
    em = (hb.group_by(["hour", "meeting"]).agg(exp_bp=(pl.col("bp") * pl.col("mid")).sum() / pl.col("mid").sum(),
                                               prob_sum=pl.col("mid").sum()).sort(["hour", "meeting"]))
    tot = em.group_by("hour").agg(total_bp=pl.col("exp_bp").sum(), n=pl.len()).filter(pl.col("n") == 3)
    out = [{"hour": r["hour"].isoformat(), "meeting": r["meeting"], "exp_bp": r["exp_bp"], "prob_sum": r["prob_sum"],
            "zq_bp": None, "gap_bp": None} for r in em.iter_rows(named=True)]
    out += [{"hour": r["hour"].isoformat(), "meeting": "All 3", "exp_bp": r["total_bp"], "prob_sum": None,
             "zq_bp": None, "gap_bp": None} for r in tot.iter_rows(named=True)]
    return out


def main() -> None:
    b = books()
    t = trades()
    b = add_targets(add_flow(b, t))
    b = b.with_columns(bucket=pl.when(pl.col("mid").is_null()).then(None)
                       .otherwise(pl.col("mid").cut([0.10, 0.40, 0.60, 0.90], labels=[x[2] for x in BUCKETS])))
    sp = spoof_events(b, t)
    if sp.height:
        sp30 = sp.select("meeting", "outcome", pl.col("ts").alias("spoof_ts")).sort("spoof_ts")
        b = b.sort("ts").join_asof(sp30.rename({"spoof_ts": "ts"}).with_columns(spoof=pl.lit(True)).sort("ts"),
                                   on="ts", by=["meeting", "outcome"], strategy="backward", tolerance="30s")
        b = b.with_columns(spoof=pl.col("spoof").fill_null(False))
    else:
        b = b.with_columns(spoof=pl.lit(False))

    rows = []
    for mt in sorted(b["meeting"].unique().to_list()):
        g = b.filter(pl.col("meeting") == mt)
        cuts = [("all", g), ("news: unknown (no calendar)", g.filter(pl.col("news").is_null()))]
        cuts += [(f"price {lab}", g.filter(pl.col("bucket").cast(pl.Utf8) == lab)) for *_, lab in BUCKETS]
        cuts += [("no vanished large order in last 30s", g.filter(~pl.col("spoof"))),
                 ("after a vanished large order", g.filter(pl.col("spoof")))]
        for split, sub in cuts:
            for sig in SIGNALS:
                for h in HORIZONS:
                    s = stats(sub, sig, h) if sub.height else {"n": 0, "n_moves": 0, "corr": None, "hit_rate": None, "n_indep": 0}
                    rows.append({"meeting": mt, "split": split, "signal": sig, "horizon": h, **s})
    first, last = b["ts"].min(), b["ts"].max()
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "signals.json").write_text(json.dumps({
        "window": [first.isoformat(), last.isoformat()], "hours": (last - first).total_seconds() / 3600,
        "snapshots": b.height, "trades": t.height, "spoof_events": sp.height, "rows": rows,
    }, default=str, allow_nan=False if False else True).replace("NaN", "null"))
    (OUT_DIR / "curve.json").write_text(json.dumps(curve(b), default=str))
    best = [r for r in rows if r["split"] == "all" and r["corr"] is not None and not math.isnan(r["corr"])]
    print(f"{b.height} snapshots, {t.height} trades, {sp.height} spoof events, {len(rows)} stat rows; "
          f"{len(best)} with a correlation")


if __name__ == "__main__":
    main()
