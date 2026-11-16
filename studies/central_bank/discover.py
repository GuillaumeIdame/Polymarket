"""Resolve the three FOMC events to outcome token ids, fee schedule and reward terms.

Writes out/markets.json. Read-only public endpoints (Gamma, CLOB rewards).
"""
from __future__ import annotations

import json

from cb_common import CLOB, GAMMA, MARKETS_FILE, MEETINGS, OUT_DIR, bp_for, http, iso


def find_event(h, title: str, date: str) -> dict | None:
    body = h.get(f"{GAMMA}/public-search", {"q": title.rstrip("?"), "limit_per_type": 20, "events_status": "active"})
    year_month = date[:7]
    for ev in body.get("events") or []:
        if ev.get("title", "").strip().lower() == title.lower() and not ev.get("closed"):
            # endDate is the day after the meeting in UTC (e.g. 2026-12-10T04:59Z for 9 Dec ET)
            if ev.get("endDate", "")[:7] == year_month:
                return h.get(f"{GAMMA}/events", {"slug": ev["slug"]})[0]
    return None


def rewards(h, cid: str) -> dict:
    try:
        rows = h.get(f"{CLOB}/rewards/markets/{cid}").get("data") or []
    except Exception as e:  # noqa: BLE001 - reported, never filled
        return {"error": str(e)}
    if not rows:
        return {"rate_per_day": None}
    r = rows[0]
    cfg = r.get("rewards_config") or []
    return {"rate_per_day": sum(c.get("rate_per_day") or 0 for c in cfg),
            "rewards_max_spread": r.get("rewards_max_spread"), "rewards_min_size": r.get("rewards_min_size")}


def main() -> None:
    h = http()
    outcomes, problems = [], []
    for m in MEETINGS:
        ev = find_event(h, m["title"], m["date"])
        if not ev:
            problems.append(f"{m['meeting']}: event '{m['title']}' not found")
            continue
        for mk in ev["markets"]:
            if mk.get("closed"):
                continue
            yes, no = json.loads(mk["clobTokenIds"])
            label = mk.get("groupItemTitle") or mk["question"]
            outcomes.append({
                "meeting": m["meeting"], "meeting_date": m["date"], "event_slug": ev["slug"],
                "event_url": f"https://polymarket.com/event/{ev['slug']}",
                "outcome": label, "bp": bp_for(label), "condition_id": mk["conditionId"],
                "yes_token": yes, "no_token": no, "tick_size": float(mk.get("orderPriceMinTickSize") or 0.01),
                "fee_schedule": mk.get("feeSchedule"), "fees_enabled": mk.get("feesEnabled"),
                "volume": float(mk.get("volumeNum") or 0), "end_date": mk.get("endDate"),
                **{f"rw_{k}": v for k, v in rewards(h, mk["conditionId"]).items()},
            })
            if bp_for(label) is None:
                problems.append(f"{m['meeting']}: unknown outcome label '{label}' (no bp mapping)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MARKETS_FILE.write_text(json.dumps({"resolved_at": iso(), "outcomes": outcomes, "problems": problems,
                                        "meetings": MEETINGS}, indent=1))
    print(f"{len(outcomes)} outcomes across {len({o['meeting'] for o in outcomes})} meetings; problems: {problems}")


if __name__ == "__main__":
    main()
