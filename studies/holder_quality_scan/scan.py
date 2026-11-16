"""Holder-quality scan, step 1: markets, holders, wallet records, tags.  (PROMPT.md §2-§5)

Writes out/scan.json. Public endpoints only (Gamma + Data API v2), max 8 parallel calls.
Usage: python3 scan.py [--per-cat 8] [--holders 20] [--min-vol 75000]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

from hq_common import (
    DATA,
    FAILURES,
    GAMMA,
    HORIZON_DAYS,
    categorize,
    get,
    pmap,
    record_features,
    save,
    smart_score,
    tag_holder,
)

DAY = 86400


def ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_events(now: float, min_vol: float) -> list[dict]:
    out = []
    for page in range(40):
        body = get(f"{GAMMA}/events", {"closed": "false", "active": "true", "order": "volume", "ascending": "false",
                                       "end_date_min": datetime.fromtimestamp(now + 2 * 3600, timezone.utc).isoformat(),
                                       "end_date_max": datetime.fromtimestamp(now + 91 * DAY, timezone.utc).isoformat(),
                                       "limit": 100, "offset": page * 100}, what="gamma_events")
        if not body:
            break
        out += body
        if len(body) < 100 or float(body[-1].get("volume") or 0) < min_vol:
            break
    return out


def select_markets(events: list[dict], now: float, per_cat: int, min_vol: float) -> list[dict]:
    cands = defaultdict(list)
    for ev in events:
        cat = categorize([t.get("label", "") for t in ev.get("tags") or []])
        if not cat:
            continue
        for m in ev.get("markets") or []:
            if m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
                continue
            end = ts(m.get("endDate"))
            vol = float(m.get("volumeNum") or 0)
            try:
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                outcomes = json.loads(m.get("outcomes") or "[]")
                tokens = json.loads(m.get("clobTokenIds") or "[]")
            except (ValueError, TypeError):
                continue
            if (end is None or end < now + 2 * 3600 or end > now + HORIZON_DAYS[cat] * DAY or vol < min_vol
                    or len(tokens) != 2 or not any(0.04 <= p <= 0.96 for p in prices)):
                continue
            cands[cat].append({
                "category": cat, "event_id": ev["id"], "event_title": ev.get("title"), "event_slug": ev.get("slug"),
                "condition_id": m["conditionId"], "question": m.get("question"), "slug": m.get("slug"),
                "end_date": m.get("endDate"), "end_ts": end, "volume": vol,
                "volume_24h": float(m.get("volume24hr") or 0), "liquidity": float(m.get("liquidityNum") or 0),
                "outcomes": outcomes, "prices": prices, "tokens": tokens,
                "neg_risk": bool(m.get("negRisk")), "fee_schedule": m.get("feeSchedule"),
                "resolution_source": m.get("resolutionSource") or ev.get("resolutionSource"),
                "url": f"https://polymarket.com/event/{ev.get('slug')}",
                "tags": [t.get("label") for t in ev.get("tags") or []],
            })
    picked = []
    for cat in HORIZON_DAYS:
        per_event = defaultdict(int)
        for m in sorted(cands[cat], key=lambda x: x["end_ts"]):
            if per_event[m["event_id"]] >= 2:
                continue
            per_event[m["event_id"]] += 1
            picked.append(m)
            if sum(per_event.values()) >= per_cat:
                break
    return picked


def fetch_holders(m: dict, n: int) -> list[dict]:
    body = get(f"{DATA}/holders", {"condition": m["condition_id"], "limit": n, "include_pnl": "true"}, "holders")
    rows = []
    for tok in (body or {}).get("data") or []:
        for hd in (tok.get("holders") or [])[:n]:
            rows.append({"condition_id": m["condition_id"], "token_id": hd.get("token_id") or tok.get("token_id"),
                         "outcome_index": hd.get("outcome_index"), "wallet": (hd.get("proxy_wallet") or "").lower(),
                         "handle": hd.get("name") if hd.get("display_username_public") else None,
                         "pseudonym": hd.get("pseudonym"), "shares": hd.get("amount") or 0.0,
                         "avg_price": hd.get("avg_price"), "cost": hd.get("entry_cost_usdc") or 0.0,
                         "current_price": hd.get("current_price"), "current_value": hd.get("current_value"),
                         "unrealized_pnl": hd.get("unrealized_pnl"), "total_pnl": hd.get("total_pnl")})
    return rows


def fetch_wallet(w: str) -> dict:
    stats = (get(f"{DATA}/user-stats", {"user": w}, "user_stats") or {}).get("data")
    vol = None
    if not stats:
        vol = (get(f"{DATA}/user-volume", {"user": w}, "user_volume") or {}).get("data")
    pnl = (get(f"{DATA}/user-pnl", {"user": w, "interval": "all", "fidelity": "1d"}, "user_pnl") or {}).get("data")
    return {"wallet": w, "stats": stats, "volume": vol, "points": (pnl or {}).get("points") or []}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-cat", type=int, default=8)
    ap.add_argument("--holders", type=int, default=20)
    ap.add_argument("--min-vol", type=float, default=75_000)
    a = ap.parse_args()
    t0 = time.time()
    now = time.time()

    events = fetch_events(now, a.min_vol)
    markets = select_markets(events, now, a.per_cat, a.min_vol)
    print(f"events {len(events)} -> markets {len(markets)} "
          f"{dict((c, sum(m['category'] == c for m in markets)) for c in HORIZON_DAYS)}")

    holders = [r for rows in pmap(lambda m: fetch_holders(m, a.holders), markets) for r in rows]
    wallets = sorted({r["wallet"] for r in holders if r["wallet"]})
    print(f"holders {len(holders)} rows, {len(wallets)} wallets")
    recs = {r["wallet"]: r for r in pmap(fetch_wallet, wallets)}

    feats_now, feats_pit = {}, {}
    for w, r in recs.items():
        feats_now[w] = record_features(r["points"], r["stats"], r["volume"], now)
        feats_pit[w] = record_features(r["points"], r["stats"], r["volume"], now - 30 * DAY)

    # hedge = holds both sides of the same market
    sides = defaultdict(set)
    for r in holders:
        sides[(r["condition_id"], r["wallet"])].add(r["outcome_index"])
    for r in holders:
        f = feats_now.get(r["wallet"])
        hedge = len(sides[(r["condition_id"], r["wallet"])]) > 1
        tag, ins, sm = tag_holder(f, r["shares"], r["cost"], r["avg_price"], hedge)
        r.update(tag=tag, insider_score=ins, smart_score=sm, hedge=hedge,
                 systematic=bool(f and (f["fills"] or 0) > 20_000))

    wallet_rows = []
    for w, r in recs.items():
        f, fp = feats_now.get(w), feats_pit.get(w)
        sm_pit, gate_pit = smart_score(fp)
        pts = sorted(r["points"], key=lambda p: p["timestamp"])
        wallet_rows.append({
            "wallet": w, "stats_null": r["stats"] is None, "features": f, "features_pit": fp,
            "smart_pit": sm_pit, "smart_pit_gate": gate_pit,
            "pit_tag": "SMART" if gate_pit and sm_pit >= 70 else "solid" if gate_pit and sm_pit >= 50 else "other",
            # compact last-120-day cumulative series for class curves
            "series": [[p["timestamp"], round(p.get("economic_pnl") or 0, 2)] for p in pts
                       if p["timestamp"] >= now - 120 * DAY],
        })

    save("scan.json", {"run_at": int(now), "params": vars(a), "n_events": len(events), "markets": markets,
                       "holders": holders, "wallets": wallet_rows, "failures": list(FAILURES),
                       "elapsed_s": round(time.time() - t0, 1)})
    tags = defaultdict(int)
    for r in holders:
        tags[r["tag"]] += 1
    print(f"done in {time.time() - t0:.0f}s; tags {dict(tags)}; failures {len(FAILURES)}")


if __name__ == "__main__":
    main()
