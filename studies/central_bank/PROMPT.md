# Central Bank: FOMC market-making study (live, paper only)

Version 1 (2026-11-16). Load with `prompts/00_desk_charter.md`; the charter's method and data rules win. Work lives in
`studies/central_bank/` ("Central Bank"); pipeline jobs `cb-recorder`, `cb-daily`, `cb-weekly`.
Status: **recorder live since 16 Nov 2026; Phases 2-3 running on placeholders until the open inputs below arrive.**

**Hard rule: paper trading only.** Never place, sign or cancel a real order, and never ask for a private key.

---

## 1. Goal
Find out whether order-book imbalance and trade-flow imbalance predict short-term price moves on Fed-decision markets,
and whether a quoting strategy built on them beats naive quoting after costs.

Hypothesis (charter §10): imbalance and flow signals predict the 30 s-15 min mid change on FOMC markets enough for an
imbalance-skewed quoter to beat naive quoting after fees and adverse selection. Other side: retail takers reacting to
headlines, and better-informed rates traders hedging or front-running releases.

## 2. Open inputs (ask before building the dependent parts)
| Input | Needed for | If missing |
|---|---|---|
| `pm_mm_backtest.py` (strategy logic + CSV schema `bid, ask, bid_size, ask_size, trade_side, trade_size, anchor, news`) | Phase 3 strategies, output schema | build Phases 1-2 only |
| ZQ futures data source | anchor | store `anchor = null`, flag it |
| US data calendar (CPI, NFP, PCE, FOMC, Fed speakers) | news flag | store `news = null`, flag it |

Stop and ask before anything that costs money or needs credentials (paid data, keyed RPC, Kalshi account).

## 3. Setup
1. Read the current Polymarket API docs before writing code. Confirm the public endpoints for order book, trades and
   websocket feeds. Don't rely on memory; the API changed in 2026. Reuse `src/pmkt/recorder.py` (CLOB websocket,
   reconnect, gap log) where it fits.
2. Markets: the next three FOMC meetings, every outcome in each. Resolve token IDs from the API.

   | Market | Meeting |
   |---|---|
   | "Fed Decision in December?" | 9 Dec 2026 |
   | "Fed Decision in January?" | 27 Jan 2027 |
   | "Fed Decision in March?" | 17 Mar 2027 |

   Record the same three meetings on Kalshi (read-only) for cross-venue comparison.

## 4. Phase 1: recorder (continuous through 17 Mar 2027)
- Every 5 s per outcome: best bid/ask, sizes at the top 5 levels, timestamp.
- Every trade: time, price, size, taker side.
- **Anchor per meeting** from ZQ (30-day Fed funds) futures, day-weighted. Each anchor is the expected move at that
  meeting only: subtract the rate implied after the previous meeting.

  | Meeting | Contract(s) | Pre-meeting rate | Cross-check |
  |---|---|---|---|
  | Dec 2026 | ZQZ6 | ZQX6 | |
  | Jan 2027 | ZQF7, ZQG7 (no Feb meeting → clean post-January rate) | | |
  | Mar 2027 | ZQH7 | ZQG7 | ZQJ7 |

- **News flag:** 1 from 5 minutes before to 5 minutes after scheduled US data (CPI, NFP, PCE, FOMC, Fed speakers).
- Write daily Parquet files. Log gaps and reconnects. Restart automatically on failure.

## 5. Phase 2: signal study (after 1 week of data, then weekly)
- Signals: top-of-book imbalance, 5-level imbalance, microprice minus mid, 5-minute trade-flow imbalance, anchor minus mid.
- For each: correlation and hit rate against mid change at 30 s, 1 m, 5 m, 15 m.
- Split: news windows vs quiet, and by outcome price bucket.
- Report **per meeting; never pool** (liquidity and signal quality differ by horizon).
- Spoofing test: do large resting orders that vanish within 30 s degrade the signal?
- Daily curve check: sum of expected moves across the three meetings on Polymarket vs the ZQ strip. Log the gap.

## 6. Phase 3: paper trading (live, alongside the recorder)
- Run the five strategies from the backtester in parallel on live data.
- Fill model: front of queue if my quote improves the best price; back of queue if it joins; picked off in full if
  the market reprices through it.
- Quote size <= 5% of each market's average top-of-book size.
- Apply the current fee schedule; estimate liquidity rewards and maker rebates from current program terms. Report P&L
  with and without them.
- When a meeting resolves, close its book at settlement value and keep the others running.

## 7. Weekly report (one page)
- Table per strategy and per meeting: P&L, fills, edge per share at fill and 5 minutes later, max drawdown,
  max inventory, P&L inside vs outside news windows.
- Signal table from Phase 2.
- Cross-venue and curve gaps observed during the week.
- One paragraph: is there evidence of edge, what would falsify it, what you would change.

## 8. Study-specific rules (on top of charter §8)
- State every assumption. If data is missing or an endpoint fails, say so; never invent values.
- Fit parameters on one period and report results on a later one. Never tune and evaluate on the same data.
- Three meetings give few independent resolution events; treat any P&L conclusion as provisional and lean on the
  per-interval signal statistics (with dependence-aware errors) for evidence.
