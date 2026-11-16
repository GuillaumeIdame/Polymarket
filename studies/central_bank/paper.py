"""Phase 3: paper market making on the recorded FOMC books.  (PROMPT.md §6)  Paper only: no orders, no keys.

The five strategies of pm_mm_backtest.py are not in the repo yet. Until that file arrives, five PLACEHOLDER
strategies run here so the fill model, costs and report are exercised end to end. Swap STRATEGIES for the
backtester's logic when it lands; parameters below are fixed in advance, not fitted.

Fill model (prompt): improve the best price -> front of queue; join -> back of queue (queue = displayed size);
market trades or reprices through the quote -> filled in full at the quote.
Quote size: 5% of the average top-of-book size seen so far (no look-ahead); skipped below the 5-share minimum.
Costs: makers pay no fee; maker rebate = rebateRate x taker fee (fee = size x rate x p x (1-p)); liquidity rewards
are a rough estimate (our size share of resting size within the max spread, only when our size >= the min size).
Writes out/paper.json.
"""
from __future__ import annotations

import bisect
import json
import math
from datetime import timedelta

import polars as pl
from cb_common import OUT_DIR, load_markets
from cb_data import books, trades

QUOTE_SHARE, MIN_ORDER = 0.05, 5.0
IMB_PULL, INV_LIMIT_X = 0.5, 3.0


def on_grid(p: float, tick: float, up: bool) -> float:
    k = p / tick
    return round((math.ceil(k - 1e-9) if up else math.floor(k + 1e-9)) * tick, 6)


def naive_join(r, tick, q, size):
    return r["bid_px_1"], r["ask_px_1"]


def naive_improve(r, tick, q, size):
    bb, ba = r["bid_px_1"], r["ask_px_1"]
    if ba - bb >= 2 * tick - 1e-9:
        return round(bb + tick, 6), round(ba - tick, 6)
    return bb, ba


def imbalance_skew(r, tick, q, size):
    bb, ba = r["bid_px_1"], r["ask_px_1"]
    imb = r["tob_imb"] or 0
    return (None if imb < -IMB_PULL else bb), (None if imb > IMB_PULL else ba)


def microprice_center(r, tick, q, size):
    bb, ba, mp = r["bid_px_1"], r["ask_px_1"], r["microprice"] or r["mid"]
    bid = min(on_grid(mp - tick / 2, tick, up=False), round(ba - tick, 6))
    ask = max(on_grid(mp + tick / 2, tick, up=True), round(bb + tick, 6))
    return max(bid, bb), min(ask, ba)


def inventory_skew(r, tick, q, size):
    bb, ba = r["bid_px_1"], r["ask_px_1"]
    lim = INV_LIMIT_X * size
    return (None if q >= lim else bb), (None if q <= -lim else ba)


STRATEGIES = {"P1 join best": naive_join, "P2 improve 1 tick": naive_improve, "P3 imbalance pull": imbalance_skew,
              "P4 microprice centre": microprice_center, "P5 inventory cap": inventory_skew}


def run_outcome(g: pl.DataFrame, tr: pl.DataFrame, meta: dict, strat) -> dict:
    tick = meta["tick_size"]
    fee = meta.get("fee_schedule") or {}
    rate, rebate_rate = float(fee.get("rate") or 0), float(fee.get("rebateRate") or 0)
    rw_day, rw_spread, rw_min = meta.get("rw_rate_per_day"), meta.get("rw_rewards_max_spread"), meta.get("rw_rewards_min_size")
    rows = g.to_dicts()
    tl = tr.to_dicts()
    ti = 0
    q = cash = rebates = rewards = 0.0
    quotes = {"bid": None, "ask": None}   # side -> [px, size_left, queue_ahead]
    fills, mtm, inv = [], [], []
    top_sum = n_top = 0
    for i, r in enumerate(rows[:-1]):
        nxt = rows[i + 1]
        if r["bid_px_1"] is None or r["ask_px_1"] is None or r["mid"] is None:
            continue
        top_sum += r["top_size"] or 0
        n_top += 1
        size = math.floor(QUOTE_SHARE * top_sum / n_top)
        if size < MIN_ORDER:
            quotes = {"bid": None, "ask": None}
        else:
            bid, ask = strat(r, tick, q, size)
            for side, px, best, lvl in (("bid", bid, r["bid_px_1"], r["bid_sz_1"]), ("ask", ask, r["ask_px_1"], r["ask_sz_1"])):
                if px is None:
                    quotes[side] = None
                    continue
                cur = quotes[side]
                improves = px > best + 1e-9 if side == "bid" else px < best - 1e-9
                if cur and abs(cur[0] - px) < 1e-9:
                    cur[1] = size
                    cur[2] = min(cur[2], lvl or 0) if not improves else 0.0
                else:
                    quotes[side] = [px, float(size), 0.0 if improves else float(lvl or 0)]
            # rough liquidity-reward estimate for this 5 s slice
            if rw_day and rw_spread and size >= (rw_min or 0):
                share = 0.0
                for side in ("bid", "ask"):
                    qq = quotes[side]
                    if qq and abs(qq[0] - r["mid"]) * 100 <= rw_spread:
                        resting = sum((r[f"{side}_sz_{k}"] or 0) for k in range(1, 6)
                                      if r[f"{side}_px_{k}"] is not None and abs(r[f"{side}_px_{k}"] - r["mid"]) * 100 <= rw_spread)
                        share += 0.5 * size / (size + resting)
                rewards += rw_day * 5 / 86400 * share
        # fills from trades in (t, t_next]
        while ti < len(tl) and tl[ti]["ts"] <= r["ts"]:
            ti += 1
        while ti < len(tl) and tl[ti]["ts"] <= nxt["ts"]:
            t = tl[ti]
            ti += 1
            side = "bid" if t["taker_yes"] == "SELL" else "ask"
            qq = quotes[side]
            if not qq or qq[1] <= 0:
                continue
            through = t["price_yes"] < qq[0] - 1e-9 if side == "bid" else t["price_yes"] > qq[0] + 1e-9
            at = abs(t["price_yes"] - qq[0]) < 1e-9
            got = 0.0
            if through:
                got = qq[1]
            elif at:
                qq[2] -= t["size"]
                if qq[2] < 0:
                    got = min(-qq[2], qq[1])
                    qq[2] = 0.0
            if got > 0:
                fills.append((r["ts"], side, qq[0], got, r["mid"], "trade"))
                qq[1] -= got
        # picked off: next snapshot reprices through a resting quote
        for side in ("bid", "ask"):
            qq = quotes[side]
            if qq and qq[1] > 0 and nxt["bid_px_1"] is not None and nxt["ask_px_1"] is not None:
                if (side == "bid" and nxt["ask_px_1"] <= qq[0] - 1e-9) or (side == "ask" and nxt["bid_px_1"] >= qq[0] + 1e-9):
                    fills.append((r["ts"], side, qq[0], qq[1], r["mid"], "repriced"))
                    qq[1] = 0
        # book the fills from this step
        while len(inv) < len(fills):
            ts, side, px, sz, mid, how = fills[len(inv)]
            q += sz if side == "bid" else -sz
            cash += -px * sz if side == "bid" else px * sz
            rebates += rebate_rate * rate * sz * px * (1 - px)
            inv.append(q)
        mtm.append((nxt["ts"], cash + q * nxt["mid"] if nxt["mid"] is not None else None))
    # edge at fill and 5 minutes later
    mids = {r["ts"]: r["mid"] for r in rows}
    ts_sorted = [r["ts"] for r in rows]
    e0 = e5 = shares = 0.0
    for ts, side, px, sz, mid, _how in fills:
        sgn = 1 if side == "bid" else -1
        e0 += sgn * (mid - px) * sz
        k = bisect.bisect_left(ts_sorted, ts + timedelta(minutes=5))
        m5 = mids[ts_sorted[k]] if k < len(ts_sorted) else None
        if m5 is not None:
            e5 += sgn * (m5 - px) * sz
        shares += sz
    vals = [v for _, v in mtm if v is not None]
    peak, mdd = -math.inf, 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return {"pnl": vals[-1] if vals else 0.0, "rebates": rebates, "rewards": rewards, "fills": len(fills),
            "repriced_fills": sum(1 for f in fills if f[5] == "repriced"), "shares": shares,
            "edge_fill": e0 / shares if shares else None, "edge_5m": e5 / shares if shares else None,
            "max_dd": mdd, "max_inv": max((abs(x) for x in inv), default=0.0), "inventory_end": q}


def main() -> None:
    b = books().sort(["meeting", "outcome", "ts"])
    t = trades()
    meta = {(m["meeting"], m["outcome"]): m for m in load_markets()}
    detail = []
    for (mt, oc), g in b.group_by(["meeting", "outcome"], maintain_order=True):
        tr = t.filter((pl.col("meeting") == mt) & (pl.col("outcome") == oc)).sort("ts") if t.height else t
        for name, fn in STRATEGIES.items():
            r = run_outcome(g, tr, meta[(mt, oc)], fn)
            detail.append({"strategy": name, "meeting": mt, "outcome": oc, **r})
    out = []
    for name in STRATEGIES:
        for mt in sorted({d["meeting"] for d in detail}):
            ds = [d for d in detail if d["strategy"] == name and d["meeting"] == mt]
            sh = sum(d["shares"] for d in ds)
            out.append({"strategy": name, "meeting": mt, "pnl": sum(d["pnl"] for d in ds),
                        "pnl_with_rebates_rewards": sum(d["pnl"] + d["rebates"] + d["rewards"] for d in ds),
                        "rebates": sum(d["rebates"] for d in ds), "rewards_est": sum(d["rewards"] for d in ds),
                        "fills": sum(d["fills"] for d in ds), "repriced_fills": sum(d["repriced_fills"] for d in ds),
                        "shares": sh,
                        "edge_fill": sum((d["edge_fill"] or 0) * d["shares"] for d in ds) / sh if sh else None,
                        "edge_5m": sum((d["edge_5m"] or 0) * d["shares"] for d in ds) / sh if sh else None,
                        "max_dd": max(d["max_dd"] for d in ds), "max_inv": max(d["max_inv"] for d in ds),
                        "pnl_news": None, "pnl_quiet": None, "news_status": "no calendar"})
    (OUT_DIR / "paper.json").write_text(json.dumps({"summary": out, "detail": detail,
                                                    "window": [str(b["ts"].min()), str(b["ts"].max())],
                                                    "trades_used": t.height}, default=str))
    print(f"{len(detail)} strategy x outcome runs; total fills {sum(d['fills'] for d in detail)}")


if __name__ == "__main__":
    main()
