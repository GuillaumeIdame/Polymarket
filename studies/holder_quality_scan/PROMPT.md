# Holder-quality scan (daily)

Version 4 (2026-11-16), restructured from v3. Load with `prompts/00_desk_charter.md`; the charter's method and data rules win
over anything here. What changed from v3 is listed at the end.

**Reference implementation:** `scan.py` (markets, holders, wallet records) then `enrich.py` (portfolio exposure, time
series, class PnL curves, replication test). The prompt works with or without them.
Run order: `python3 scan.py && python3 enrich.py`, then publish `art/*.json` to the dashboard.

---

## 1. Task
For a given day, scan live Polymarket markets in five categories, grade every top holder on wallet quality and
portfolio exposure, test whether the good ones are copyable, and flag insider-like and smart-money positioning.
Evidence, not accusations.

House principles for this scan (charter §2 applies in full):
- Prefer a wallet with one dominant category and many markets over a generalist or a one-bet story.
- A "smart" tag is not a copy signal; never recommend following a wallet without the replication test (§7).
- Fresh wallets with size on low-probability outcomes get immediate attention and immediate skepticism
  (this signal is still untested on our data, see charter appendix).
- Note settlement quirks per market type (charter §3: 15-min/4-h crypto on a 60 s TWAP, daily crypto on a single
  1-min candle close, ladders resolve on touch).
- Maker rebates and LP rewards are part of some wallets' PnL; down-weight high-fill bots as directional signals.

## 2. Scope
Categories (first match wins; elections always go to Politics):

| Category | Gamma tags | Look-ahead |
|---|---|---|
| Sports | Sports, Games, Esports, Soccer, UFC, NFL/CFB | 4 d |
| Crypto | Crypto, Crypto Prices, Bitcoin, Ethereum, Solana | 21 d |
| Politics | Elections, Global Elections, Main Election, Politics, Trump, Congress, Courts | 45 d |
| Geopolitics | Geopolitics, Iran, Middle East, Ukraine, Russia, Military, Israel, Peace Deal, Hormuz | 90 d |
| Macro | Economy, Macro Indicators, Fed/Fed Rates, Inflation, Global Rates, Economic Policy, Oil, Stocks, Finance | 90 d |

Per category: the 8 nearest-expiry markets (sort by market-level `endDate`), max 2 per event, volume >= $75k,
expiry >= 2 h out. Drop markets with no outcome price between 4% and 96%.

## 3. Data recipe (no key; Data API v2 only, v1 is retired)
| Need | Endpoint |
|---|---|
| Markets | `gamma-api.polymarket.com/events` with `end_date_min/max`, `order=volume`, header `Accept-Encoding: gzip` |
| Holders | `data-api.polymarket.com/v2/holders?condition=&limit=20&include_pnl=true` |
| Wallet record | `/v2/user-stats` (null for ~6% of new wallets → fall back to `/v2/user-volume`), `/v2/user-pnl?interval=all&fidelity=1d` |
| Portfolio exposure | `/v2/value?user=` (total value); `/v2/positions?user=&status=OPEN&sort_by=CURRENT_VALUE&limit=100` (open positions with `first_entry_at`, `event_id`, `end_date`) |
| Replication test | `/v2/positions?user=&status=CLOSED\|REDEEMABLE\|REDEEMABLE_LOST&start=<now-30d>`; settled rows zero their cost fields, so rebuild basis as `avg_price * total_size`; PnL is `total_pnl`. Cross-check with `/v2/activity` + `/v2/resolutions` (§7) |
| Time series | `/v2/prices-history?token_id=&interval=1w&bucket_seconds=3600`; `/v2/trades?condition=&limit=1000` (cursor pages, last 72 h) |

Max 8 parallel calls; retry 429 and 5xx. About 1 minute per full run.

## 4. Per holder, report
- Shares, average entry, cost, unrealized PnL, tag.
- Portfolio value, % of portfolio in this bet, % in the same event, number of open positions, top-1 and top-3 share,
  % of portfolio expiring within 7 days.
- Hours held; entry vs current price.
- Wallet age, trades, PnL, Sharpe, win-day rate.

Per side: weighted average entry (only holders with a real cost basis), top-5 share, and **smart lean** =
SMART + solid shares as a fraction of directional smart shares.

## 5. Wallet tags (first hit wins)
1. **no-basis:** 1,000+ shares at under $1 cost (transfers, contracts). Ignore.
2. **Market-maker / arb:** holds both sides, or lifetime volume > $5M with |ROI| < 0.5%.
3. **INSIDER-LIKE** (pattern score >= 60 of 100; gate: ticket >= $2,000, not a hedge). 40-59 = **watch-fresh**.

   | Factor | Points |
   |---|---|
   | Wallet age <= 7 d / <= 30 d / <= 90 d | +30 / +20 / +8 |
   | Lifetime trades <= 20 / <= 100 | +15 / +8 |
   | This market is >= 50% / 25-50% of lifetime volume | +20 / +10 |
   | Ticket >= $10k / >= $5k | +10 / +5 |
   | >= $3k at <= 30 cents | +15 |

   Strengthen the flag when the bet is >= 50% of the wallet's portfolio (observed median for flagged wallets 78%,
   vs 1% for smart wallets).
4. **SMART** (record score >= 70 of 100; gate: 60 active days, 100 trades, $50k all-time PnL). 50-69 = **solid**.

   | Factor | Points |
   |---|---|
   | Age >= 120 d | +10 |
   | Trades >= 300 | +10 |
   | PnL >= $25k | +20 |
   | ROI >= 2% | +10 |
   | Sharpe >= 1.5 | +20 |
   | Win-days >= 55% | +10 |
   | >= 65% of rolling 30 d windows positive | +10 |
   | No lucky day (top day <= 35% of gains) | +5 |
   | Drawdown <= 40% | +5 |

   Above 20k fills = systematic (bot/quant): keep, down-weight.
5. **other.**

## 6. Output (always this order)
1. **Quick snapshot:** counts, one-sentence takeaway, time window, category mix.
2. **Edge diagnosis:** specialization, entry-price distribution, timing, concentration. Charts: 90-day PnL by wallet
   group; smart lean vs market price; wallet age vs trades; portfolio share in the bet by group.
3. **Market explorer:** markets by category ordered by expiry. Per market: 7-day price with each side's holder entry
   lines, 72-hour cumulative flow by wallet group, both sides' holders.
4. **Replication test:** smart-tagged wallets mirrored over the last 30 days with slippage; pass/fail verdict.
5. **Risks and red flags:** fresh-wallet clusters, single-bet dependency, thin markets, settlement quirks,
   over-diversification.
6. **Actionable takeaway:** concrete watch-list rule and a plain Polymarket link; never advice to copy blindly.

## 7. Replication test
Mirror every position settled in the last 30 days pro-rata, 1 cent per share entry slippage, no compounding,
$400 notional. Per wallet report: positions, distinct events, win rate, cost, PnL, ROI, net ROI, mirrored $400
outcome, main category share, best-win share.

**Pass** = 30+ positions, net ROI > 5%, one category >= 60%, no single win > 40% of gains. Say plainly how many
smart-tagged wallets fail. First run: 67 of 153 stayed profitable, 16 passed everything, median net ROI -2%.

Integrity checks (charter §6 and §8):
- **Winner bias:** `/v2/positions` drops losing positions on inactive markets. Compute the sample's excess win per share;
  if it exceeds 2c, rebuild from `/v2/activity` + `/v2/resolutions` or mark the result unreliable.
- **Hindsight:** tags built from all-time stats include the 30-day test window. Score wallets on data up to
  `now - 30d` where the API allows; otherwise label the result **in-sample upper bound**.
- Report median and with/without the single best wallet, not just the pass count.

## 8. Signals to highlight
- **Fresh-wallet cluster:** 3+ insider-like wallets on one side. Check whether rival outcomes both carry fresh money
  (coordination or capital splitting, not information).
- **Watch-list rule:** smart-lean side priced < 50%, lean >= 75%, volume >= $100k, at least 3 smart wallets.
- **Flow divergence:** smart group buying while the crowd sells in the 72 h chart.
- **Concentration:** top-5 share of a side; flagged wallets with >= 50% of portfolio in one event.

## 9. Guardrails
- "Insider-like" is a statistical pattern, never proof. New users and whales splitting capital look identical.
- Wallets outside the top holders appear as "other" in flow; the flow chart is only as good as that coverage.
- Sharpe from marked-to-market daily PnL is noisy; under 60 active days treat it as unknown.
- The replication test ignores latency and fill quality; it is an upper bound.
- Title-based specialization is a keyword heuristic.
- No wallet is a trading signal alone. Show address prefix plus public handle only.
- Report API failures and null fields; never fill gaps.

## 10. Tuning knobs
`--per-cat`, `--holders`, `--min-vol`, `HORIZON`, insider ticket gate ($2k), SMART PnL gate ($50k), slippage (1 cent),
copy-pass thresholds.

---

### Changes from v3
- Shared rules (no referral links, settlement, method rules) moved to the charter; this file keeps scan-specific rules.
- Crypto settlement corrected: "averaged price over the last 30-60 s" → 60 s TWAP on 15-min/4-h; daily still settles
  on a single 1-min candle (verified 2026-11-14).
- Replication test gains two integrity checks from the charter: winner-bias check on `/v2/positions` and the hindsight
  (in-sample) label.
- "Fresh wallets with size on longshots" now noted as untested on our data.
- Source renamed from "Saurav knowledge base" to "practitioner playbook" for consistency with the charter.
