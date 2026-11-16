"""Holder-quality scan, step 2: portfolio exposure, time series, class PnL curves, replication test, signals.

Reads out/scan.json, writes out/enriched.json and art/*.json (dashboard datasets, {rows, meta}).
Usage: python3 enrich.py [--slippage 0.01] [--notional 400]
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import time
from collections import Counter, defaultdict
from pathlib import Path

from hq_common import ART, DATA, FAILURES, TAG_ORDER, get, load, pmap, save, title_category

DAY = 86400
SMARTISH = {"SMART", "solid"}


# ---------------------------------------------------------------- fetchers
def paged(url: str, params: dict, what: str, max_pages: int, stop=None) -> tuple[list[dict], bool]:
    """Follow v2 cursors. Returns (rows, complete)."""
    rows, p = [], dict(params)
    for _ in range(max_pages):
        body = get(url, p, what)
        if body is None:
            return rows, False
        data = body.get("data") or []
        rows += data
        cur = (body.get("pagination") or {}).get("next_cursor")
        if not cur or (stop and data and stop(data[-1])):
            return rows, True
        p["cursor"] = cur
    return rows, False


def fetch_portfolio(w: str) -> dict:
    val = (get(f"{DATA}/value", {"user": w}, "value") or {}).get("data") or {}
    body = get(f"{DATA}/positions", {"user": w, "status": "OPEN", "sort_by": "CURRENT_VALUE", "limit": 100},
               "positions_open")
    return {"wallet": w, "value": val.get("value"), "open": (body or {}).get("data") or [],
            "open_more": bool(((body or {}).get("pagination") or {}).get("has_more")), "ok": body is not None}


def fetch_market_series(m: dict, now: float) -> dict:
    ph = (get(f"{DATA}/prices-history", {"token_id": m["tokens"][0], "interval": "1w", "bucket_seconds": 3600},
              "prices_history") or {}).get("data") or []
    cutoff = now - 72 * 3600
    trades, complete = paged(f"{DATA}/trades", {"condition": m["condition_id"], "limit": 1000}, "trades", 20,
                             stop=lambda last: (last.get("timestamp") or 0) < cutoff)
    trades = [t for t in trades if (t.get("timestamp") or 0) >= cutoff]
    return {"condition_id": m["condition_id"], "prices": [(p["timestamp"], p["price"]) for p in ph],
            "trades": trades, "trades_complete": complete}


def fetch_settled(w: str, start: int) -> dict:
    rows, ok = {}, True
    for status in ("CLOSED", "REDEEMABLE", "REDEEMABLE_LOST"):
        # sort_by=TIMESTAMP (newest first): the default order is by PnL, so a capped read keeps only winners.
        # With time order a cap drops the oldest rows instead, which shortens the window without biasing outcomes.
        got, complete = paged(f"{DATA}/positions", {"user": w, "status": status, "start": start, "limit": 500,
                                                    "sort_by": "TIMESTAMP"}, f"positions_{status.lower()}", 30)
        ok &= complete
        for r in got:
            rows.setdefault(r["token_id"], r)  # REDEEMABLE and REDEEMABLE_LOST overlap; dedupe by token
    act, act_complete = paged(f"{DATA}/activity", {"user": w, "type": "TRADE", "start": start, "limit": 500},
                              "activity", 6)
    return {"wallet": w, "settled": list(rows.values()), "settled_complete": ok,
            "activity_conditions": sorted({a["condition_id"] for a in act if a.get("side") == "BUY"}),
            "activity_complete": act_complete}


# ---------------------------------------------------------------- replication (PROMPT §7)
def replicate(s: dict, open_conditions: set[str], slippage: float, notional: float) -> dict:
    rows = [r for r in s["settled"] if (r.get("avg_price") or 0) > 0 and (r.get("total_size") or 0) > 0]
    basis = [r["avg_price"] * r["total_size"] for r in rows]
    pnl = [r.get("total_pnl") or 0.0 for r in rows]
    cost = sum(basis)
    out = {"wallet": s["wallet"], "positions": len(rows), "events": len({r.get("event_id") for r in rows}),
           "settled_complete": s["settled_complete"]}
    if not rows or cost <= 0:
        return {**out, "verdict": "no settled positions", "pass": False}
    scale = notional / cost
    shares_scaled = sum(r["total_size"] for r in rows) * scale
    mirror_pnl = sum(pnl) * scale - shares_scaled * slippage
    cats = Counter()
    for r, b in zip(rows, basis, strict=True):
        cats[title_category(r.get("title"), r.get("event_slug"))] += b
    main_cat, main_cost = cats.most_common(1)[0]
    gains = sum(p for p in pnl if p > 0)
    best = max(pnl)
    # resolved rows only: excess win per share (payout - entry); 1.0 = won, 0.0 = lost
    res = [r for r in rows if r.get("current_price") in (0, 0.0, 1, 1.0)]
    sz = sum(r["total_size"] for r in res)
    excess = sum((r["current_price"] - r["avg_price"]) * r["total_size"] for r in res) / sz if sz else None
    settled_conds = {r["condition_id"] for r in s["settled"]}
    traded = set(s["activity_conditions"])
    missing = traded - settled_conds - open_conditions
    out.update({
        "win_rate": sum(1 for p in pnl if p > 0) / len(pnl), "cost": cost, "pnl": sum(pnl),
        "roi": sum(pnl) / cost, "net_roi": mirror_pnl / notional, "mirror_end": notional + mirror_pnl,
        "main_category": main_cat, "main_cat_share": main_cost / cost,
        "best_win_share": best / gains if gains > 0 else None, "excess_win_c": excess * 100 if excess is not None else None,
        "coverage": 1 - len(missing) / len(traded) if traded else None, "activity_complete": s["activity_complete"],
    })
    reasons = []
    if out["positions"] < 30:
        reasons.append("<30 positions")
    if out["net_roi"] <= 0.05:
        reasons.append("net ROI <= 5%")
    if out["main_cat_share"] < 0.60:
        reasons.append("no category >= 60%")
    if out["best_win_share"] is None or out["best_win_share"] > 0.40:
        reasons.append("one win > 40% of gains" if out["best_win_share"] else "no gains")
    out["pass"] = not reasons
    out["verdict"] = "pass" if not reasons else "; ".join(reasons)
    return out


def summarize(reps: list[dict], label: str, notional: float) -> dict:
    r = [x for x in reps if x.get("net_roi") is not None]
    ends = sorted(x["mirror_end"] for x in r)
    best = max(ends) if ends else None
    without = [e for e in ends if e != best] if ends else []
    return {"set": label, "wallets": len(reps), "scored": len(r),
            "profitable": sum(1 for x in r if x["net_roi"] > 0), "passed": sum(1 for x in r if x["pass"]),
            "median_net_roi": st.median(x["net_roi"] for x in r) if r else None,
            "iqr_low": st.quantiles([x["net_roi"] for x in r], n=4)[0] if len(r) >= 4 else None,
            "iqr_high": st.quantiles([x["net_roi"] for x in r], n=4)[2] if len(r) >= 4 else None,
            "mean_end": st.mean(ends) if ends else None,
            "mean_end_ex_best": st.mean(without) if without else None, "notional": notional}


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--notional", type=float, default=400)
    a = ap.parse_args()
    t0 = time.time()
    scan = load("scan.json")
    now = scan["run_at"]
    markets, holders = scan["markets"], scan["holders"]
    W = {w["wallet"]: w for w in scan["wallets"]}
    mk = {m["condition_id"]: m for m in markets}

    # 1. portfolio exposure
    wallets = sorted({r["wallet"] for r in holders})
    port = {p["wallet"]: p for p in pmap(fetch_portfolio, wallets)}
    for r in holders:
        p = port.get(r["wallet"]) or {}
        val, opens = p.get("value"), p.get("open") or []
        m = mk[r["condition_id"]]
        this = next((o for o in opens if o["token_id"] == r["token_id"]), None)
        vals = sorted((o.get("current_value") or 0 for o in opens), reverse=True)
        r.update(
            portfolio_value=val, n_open=len(opens), n_open_more=p.get("open_more", False),
            pct_bet=(r["current_value"] or 0) / val if val else None,
            pct_event=sum(o.get("current_value") or 0 for o in opens if str(o.get("event_id")) == str(m["event_id"])) / val
            if val else None,
            top1=vals[0] / val if val and vals else None, top3=sum(vals[:3]) / val if val and vals else None,
            exp7=sum(o.get("current_value") or 0 for o in opens
                     if o.get("end_date") and time.mktime(time.strptime(o["end_date"][:10], "%Y-%m-%d")) <= now + 7 * DAY) / val
            if val else None,
            hours_held=(now - this["first_entry_at"]) / 3600 if this and this.get("first_entry_at") else None,
            entry_vs_now=(r["current_price"] - r["avg_price"]) if r.get("avg_price") is not None and r.get("current_price") is not None else None,
        )
        f = (W.get(r["wallet"]) or {}).get("features") or {}
        r.update(age_days=f.get("age_days"), trades=f.get("trades"), fills=f.get("fills"), wallet_pnl=f.get("pnl"),
                 sharpe=f.get("sharpe") if (f.get("active_days") or 0) >= 60 else None, win_days=f.get("win_days"),
                 active_days=f.get("active_days"), roi=f.get("roi"),
                 strong_flag=r["tag"] == "INSIDER-LIKE" and (r["pct_bet"] or 0) >= 0.5)

    # 2. per-side aggregates
    by_side = defaultdict(list)
    for r in holders:
        by_side[(r["condition_id"], r["outcome_index"])].append(r)
    sides = []
    for m in markets:
        smart_sh = {}
        for i in (0, 1):
            rs = by_side[(m["condition_id"], i)]
            smart_sh[i] = sum(r["shares"] * (0.5 if r["systematic"] else 1.0) for r in rs if r["tag"] in SMARTISH)
        tot_smart = smart_sh[0] + smart_sh[1]
        for i in (0, 1):
            rs = by_side[(m["condition_id"], i)]
            basis = [r for r in rs if r["tag"] != "no-basis" and r.get("avg_price") is not None and r["cost"] >= 1]
            tot = sum(r["shares"] for r in rs)
            top5 = sum(sorted((r["shares"] for r in rs), reverse=True)[:5])
            sides.append({
                "condition_id": m["condition_id"], "outcome_index": i, "outcome": m["outcomes"][i],
                "price": m["prices"][i], "holders": len(rs),
                "wavg_entry": sum(r["avg_price"] * r["shares"] for r in basis) / sum(r["shares"] for r in basis) if basis else None,
                "top5_share": top5 / tot if tot else None,
                "smart_lean": smart_sh[i] / tot_smart if tot_smart else None,
                "n_smart": sum(1 for r in rs if r["tag"] in SMARTISH),
                "n_insider": sum(1 for r in rs if r["tag"] == "INSIDER-LIKE"),
                "n_fresh": sum(1 for r in rs if r["tag"] in ("INSIDER-LIKE", "watch-fresh")),
                "smart_shares": smart_sh[i], "shares_top20": tot,
            })
    side_ix = {(s["condition_id"], s["outcome_index"]): s for s in sides}

    # 3. time series: price (outcome 0) and 72h flow by wallet group
    series = {s["condition_id"]: s for s in pmap(lambda m: fetch_market_series(m, now), markets)}
    group_of = {(r["condition_id"], r["wallet"]): r["tag"] for r in holders}
    prices_rows, flow_rows, flow_tot = [], [], {}
    for cid, s in series.items():
        for t, p in s["prices"]:
            prices_rows.append({"condition_id": cid, "ts": t, "price": p})
        start = now - 72 * 3600
        buckets = defaultdict(float)
        covered = total = 0.0
        for t in s["trades"]:
            usd = (t.get("size") or 0) * (t.get("price") or 0)
            sign = (1 if t.get("outcome_index") == 0 else -1) * (1 if t.get("side") == "BUY" else -1)
            g = group_of.get((cid, (t.get("proxy_wallet") or "").lower()), "other")
            hour = int((t["timestamp"] - start) // 3600)
            buckets[(g, hour)] += sign * usd
            total += usd
            covered += usd if (cid, (t.get("proxy_wallet") or "").lower()) in group_of else 0
        for g in TAG_ORDER:
            cum = 0.0
            if not any(k[0] == g for k in buckets):
                continue
            for hr in range(72):
                cum += buckets.get((g, hr), 0.0)
                flow_rows.append({"condition_id": cid, "ts": int(start + (hr + 1) * 3600), "group": g, "cum_usd": round(cum, 2)})
        flow_tot[cid] = {"trades_72h": len(s["trades"]), "usd_72h": total,
                         "holder_coverage": covered / total if total else None, "complete": s["trades_complete"]}

    # 4. class PnL curves (90 d), wallet group = tag of the wallet's largest position in the scan
    wgroup = {}
    for r in sorted(holders, key=lambda x: x["cost"]):
        wgroup[r["wallet"]] = r["tag"]
    day0 = (now - 90 * DAY) // DAY
    per = defaultdict(lambda: defaultdict(list))
    for w, g in wgroup.items():
        pts = {int(t // DAY): v for t, v in (W.get(w) or {}).get("series", [])}
        if day0 not in pts:
            continue
        base = pts[day0]
        for d, v in pts.items():
            if d >= day0:
                per[g][d].append(v - base)
    curve_rows = [{"date": time.strftime("%Y-%m-%d", time.gmtime(d * DAY)), "group": g,
                   "median_pnl": round(st.median(v), 2), "mean_pnl": round(st.mean(v), 2), "wallets": len(v)}
                  for g, days in per.items() for d, v in sorted(days.items())]

    # 5. replication test: smart-tagged now (in-sample) and SMART as of now-30d (point in time)
    smart_now = sorted({r["wallet"] for r in holders if r["tag"] == "SMART"})
    smart_pit = sorted(w for w, x in W.items() if x["pit_tag"] == "SMART")
    targets = sorted(set(smart_now) | set(smart_pit))
    settled = {s["wallet"]: s for s in pmap(lambda w: fetch_settled(w, int(now - 30 * DAY)), targets)}
    reps = []
    for w in targets:
        opens = {o["condition_id"] for o in (port.get(w) or {}).get("open") or []}
        rr = replicate(settled[w], opens, a.slippage, a.notional)
        hr = max((r for r in holders if r["wallet"] == w), key=lambda r: r["cost"])
        rr.update(handle=hr.get("handle") or hr.get("pseudonym"), wallet_prefix=w[:8],
                  smart_now=w in smart_now, smart_pit=w in smart_pit, systematic=hr["systematic"])
        reps.append(rr)
    rep_summary = [summarize([r for r in reps if r["smart_now"]], "Tagged SMART today (in-sample)", a.notional),
                   summarize([r for r in reps if r["smart_pit"]], "SMART as of 30 days ago (point in time)", a.notional)]
    cov = [r["coverage"] for r in reps if r.get("coverage") is not None and r.get("activity_complete")]
    exc = [r["excess_win_c"] for r in reps if r.get("excess_win_c") is not None]
    integrity = {"wallets_with_full_activity": len(cov), "median_coverage": st.median(cov) if cov else None,
                 "min_coverage": min(cov) if cov else None,
                 "median_excess_win_c": st.median(exc) if exc else None}

    # 6. signals (PROMPT §8)
    signals = []
    for m in markets:
        cid = m["condition_id"]
        s0, s1 = side_ix[(cid, 0)], side_ix[(cid, 1)]
        for s, o in ((s0, s1), (s1, s0)):
            if s["n_insider"] >= 3:
                both = o["n_fresh"] >= 2
                signals.append({"type": "Fresh-wallet cluster", "condition_id": cid, "side": s["outcome"],
                                "detail": f"{s['n_insider']} insider-like wallets"
                                          + (f"; rival side also has {o['n_fresh']} fresh wallets (capital splitting?)" if both else "")})
            if (s["price"] < 0.5 and (s["smart_lean"] or 0) >= 0.75 and m["volume"] >= 100_000 and s["n_smart"] >= 3):
                signals.append({"type": "Watch-list", "condition_id": cid, "side": s["outcome"],
                                "detail": f"lean {s['smart_lean']:.0%} at {s['price']:.0%}, {s['n_smart']} smart wallets"})
            if s["top5_share"] and s["top5_share"] >= 0.8:
                signals.append({"type": "Concentration", "condition_id": cid, "side": s["outcome"],
                                "detail": f"top-5 hold {s['top5_share']:.0%} of top-20 shares"})
        last = {}
        for fr in flow_rows:
            if fr["condition_id"] == cid:
                last[fr["group"]] = fr["cum_usd"]
        smart_flow = last.get("SMART", 0) + last.get("solid", 0)
        crowd = last.get("other", 0)
        if abs(smart_flow) >= 1000 and smart_flow * crowd < 0:
            side = m["outcomes"][0] if smart_flow > 0 else m["outcomes"][1]
            signals.append({"type": "Flow divergence", "condition_id": cid, "side": side,
                            "detail": f"smart net ${abs(smart_flow):,.0f} toward {side}, crowd ${abs(crowd):,.0f} the other way (72 h)"})
    for r in holders:
        if r["tag"] == "INSIDER-LIKE" and (r["pct_event"] or 0) >= 0.5:
            signals.append({"type": "Concentrated insider-like wallet", "condition_id": r["condition_id"],
                            "side": mk[r["condition_id"]]["outcomes"][r["outcome_index"]],
                            "detail": f"{r['wallet'][:8]} has {r['pct_event']:.0%} of portfolio in this event, ticket ${r['cost']:,.0f}"})

    save("enriched.json", {"run_at": now, "holders": holders, "sides": sides, "flow_meta": flow_tot,
                           "replication": reps, "rep_summary": rep_summary, "integrity": integrity,
                           "signals": signals, "failures_enrich": list(FAILURES)})
    export(scan, holders, sides, prices_rows, flow_rows, flow_tot, curve_rows, reps, rep_summary, integrity,
           signals, a, t0)


# ---------------------------------------------------------------- dashboard export
def code_meta(extra: list[dict]) -> list[dict]:
    here = Path(__file__).parent
    return extra + [{"label": n, "value": (here / n).read_text(), "type": "code", "lang": "python"}
                    for n in ("scan.py", "enrich.py", "hq_common.py")]


def r4(x):
    return round(x, 4) if isinstance(x, float) else x


def export(scan, holders, sides, prices_rows, flow_rows, flow_tot, curve_rows, reps, rep_summary, integrity,
           signals, a, t0):
    ART.mkdir(parents=True, exist_ok=True)
    run = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(scan["run_at"]))
    src = [{"label": "Run", "value": run}, {"label": "Source", "value": "Polymarket Gamma API + Data API v2 (public, no key)"}]
    side_ix = {(s["condition_id"], s["outcome_index"]): s for s in sides}
    tags = Counter(r["tag"] for r in holders)
    fails = scan["failures"] + list(FAILURES)
    cats = Counter(m["category"] for m in scan["markets"])
    files = {
        "run": [{"run_at": run, "markets": len(scan["markets"]), "events_scanned": scan["n_events"],
                 "holder_rows": len(holders), "wallets": len({r['wallet'] for r in holders}),
                 **{f"tag_{k.replace('-', '_').replace('/', '_').lower()}": tags.get(k, 0) for k in TAG_ORDER},
                 **{f"cat_{c.lower()}": cats.get(c, 0) for c in ("Sports", "Crypto", "Politics", "Geopolitics", "Macro")},
                 "api_failures": len(fails), "min_vol": scan["params"]["min_vol"],
                 "elapsed_s": round(scan["elapsed_s"] + time.time() - t0)}],
        "markets": [{"condition_id": m["condition_id"], "category": m["category"], "question": m["question"],
                     "event": m["event_title"], "end_date": m["end_date"], "volume": round(m["volume"]),
                     "outcome0": m["outcomes"][0], "outcome1": m["outcomes"][1],
                     "price0": m["prices"][0], "price1": m["prices"][1], "url": m["url"],
                     **{f"{k}{i}": r4(side_ix[(m['condition_id'], i)][k]) for i in (0, 1)
                        for k in ("wavg_entry", "top5_share", "smart_lean", "n_smart", "n_insider")},
                     "trades_72h": flow_tot[m["condition_id"]]["trades_72h"],
                     "holder_flow_coverage": r4(flow_tot[m["condition_id"]]["holder_coverage"]),
                     "neg_risk": m["neg_risk"]} for m in scan["markets"]],
        "holders": [{"key": f"{r['condition_id'][:10]}-{r['outcome_index']}-{r['wallet'][:10]}",
                     "condition_id": r["condition_id"], "side": r["outcome_index"], "wallet": r["wallet"][:8],
                     "handle": r.get("handle") or r.get("pseudonym") or "", "tag": r["tag"],
                     "systematic": r["systematic"], "strong_flag": r["strong_flag"],
                     **{k: r4(r.get(k)) for k in ("shares", "avg_price", "cost", "unrealized_pnl", "portfolio_value",
                                                   "pct_bet", "pct_event", "n_open", "top1", "top3", "exp7",
                                                   "hours_held", "entry_vs_now", "age_days", "trades", "wallet_pnl",
                                                   "sharpe", "win_days", "insider_score", "smart_score")}}
                    for r in holders],
        "prices": prices_rows, "flow": flow_rows, "pnl_curves": curve_rows,
        "replication": [{k: r4(v) for k, v in r.items() if k != "wallet"} for r in reps],
        "rep_summary": [{k: r4(v) for k, v in s.items()} for s in rep_summary],
        "signals": signals or [],
        "failures": [{"what": f["what"], "error": f["error"][:160]} for f in fails],
        "integrity": [{k: r4(v) for k, v in integrity.items()}],
    }
    notes = {"prices": "Hourly price of outcome 0 (first listed outcome), last 7 days, /v2/prices-history.",
             "flow": "Cumulative net USD into outcome 0 over 72 h, by wallet group; wallets outside the top-20 holders count as 'other'.",
             "replication": f"Settled positions in the last 30 days mirrored pro-rata to ${a.notional:.0f}, {a.slippage * 100:.0f} c/share slippage, no compounding.",
             "pnl_curves": "Change in cumulative economic PnL since 90 days ago, per wallet group (group = tag of the wallet's largest scanned position)."}
    for name, rows in files.items():
        meta = src + ([{"label": "Note", "value": notes[name]}] if name in notes else [])
        if name in ("run", "replication"):
            meta = code_meta(meta)
        (ART / f"{name}.json").write_text(json.dumps({"rows": rows, "meta": meta}, separators=(",", ":"), default=str))
    print(f"export: {', '.join(f'{k}={len(v)}' for k, v in files.items())}")


if __name__ == "__main__":
    main()
