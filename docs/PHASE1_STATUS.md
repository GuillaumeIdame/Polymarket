# Phase 1 status: 2026-11-14

**Scope:** recorder + on-chain fills + market/wallet/position store.
**State:** built, live-tested in a cloud session. **Not yet running 24/7.** It needs an always-on host (see `docker-compose.yml`).

## Live test results (2026-11-14, ~13:00–14:00 UTC)

| Component | Result |
|---|---|
| Gamma snapshot (slim) | 19.5k open events, 222.8k open markets, ~70 s, 57 MB, peak RSS 2.4 GB |
| Recorder universe | 2.1k candidates → 400 markets / 800 assets in 9 s (164 MB RSS). Mix: 180 sports, 82 crypto, 60 economics, 39 politics, 16 geopolitics, 23 other |
| WS recorder, 5 min | 2.04M rows, 4 connections, 0 drops, 315 MB RSS. Lag recv−exchange: avg 0.3 s, p99 3.6 s |
| Storage after slimming | 15.6 MB / 5 min ≈ **4.5 GB/day** for 800 assets (was 31 GB/day) |
| Book reconstruction vs REST `/book` | **20/20 top-of-book exact**; 29/2,016 levels (1.4%) differ, consistent with books moving between recorder stop and the REST call |
| Chain fills, 1 h (1,800 blocks) | 127k fills, 16 s (public RPC). ≈ 6.5 min to backfill one day |
| Holders (top 60 markets) | 27.3k holder rows with avg entry/PnL, 37 s |
| Position book (1 h window) | 9.8k wallets, 41.9k wallet-token rows; 280 wallets >90% maker fills (≥50 fills), the market-maker candidates |

Message flow by category (5 min): crypto 60%, sports 34%, politics 3%, economics 1%. A single BTC 15-min Up/Down market = 7% of all updates.

## Known gaps / limitations
1. **Not persistent yet.** No 24/7 host, so every day without one is lost L2 history.
2. Position book is **fills-only**. Split/merge/redeem/convert and wallet-to-wallet transfers are indexed (`chain_transfers`) but not yet folded into positions. Oversells are flagged (`unexplained_sell_shares`), not hidden.
3. Fill history only from block ~97.30M (2026-11-14 13:03 UTC). Backfill to cover a test window of 100+ resolved events, e.g. 90 days ≈ 10 h on public RPC.
4. Holders = top 400/outcome (API cap). Share-of-supply needs on-chain supply (not yet computed).
5. Recorder book check is consistent with timing but not proven at the same timestamp. Next step: compare the book `hash`.
6. Postgres not deployed. DuckDB over Parquet is the store for now; schema for the relational layer is in `sql/schema.sql`.
7. ~~Protocol V2 lifecycle events not decoded~~ **Done 2026-11-14** (`pmkt chain lifecycle`, 45 live event types,
   all checks pass; see DATA_SOURCES.md). Not yet folded into the position book (item 2).

## Next steps (in order)
1. Deploy `docker compose up -d` on a VPS (≈ $10–20/mo, 200 GB) **this week**, plus daily sync to object storage.
2. Keyed Polygon RPC (Alchemy/QuickNode) + 90-day fills backfill.
3. Fold transfers + split/merge/redeem into the position book; reconcile vs `/v2/positions` (target > 99% match).
4. Re-run the V2 checks on the first binary V2 markets (11-23 08:30 ET crypto Up/Down): fills vs Data API,
   PositionManager balances vs `/v2/positions`, `ConditionResolved` vs Gamma resolution.
5. Phase 2: holders/positions dashboard + wallet scoring (all smart-money definitions) on the walk-forward split.

## Smart-money model & expiry screener (2026-11-14)

**v1 (killed):** built on /v2/positions history. Every definition "validated" (top fifth +11c/share net), but the
sample itself showed +6.7c to +11.6c excess win per share: the API drops losing positions on inactive markets.
**v2:** /v2/activity trades (losers included) + /v2/resolutions payouts; 1,600 wallets (random holders + random on-chain
traders), 60 days, 1.41M resolved trades, 95.7k events. Sample bias check: +0.85c (passes the 2c gate).

| Definition | Rank corr. out of sample | p (BH) | Top fifth net of copy costs | Verdict |
|---|---|---|---|---|
| PnL skill | +0.17 | <0.001 | -2.1c | predictive, not tradable |
| Calibration | +0.06 | 0.25 | -3.4c | not predictive |
| Longshot | -0.06 | 0.27 | -4.4c | not predictive |
| Conviction | +0.03 | 0.47 | -3.2c | not predictive |

**No copy-trading edge.** PnL skill persists and is usable as an adverse-selection filter / alert feature.
Screener: 1,500 highest-volume markets ending within 72h (cap), 317 near resolution, 11 with insider-signature holders.
Smart positioning covers almost no screener markets yet: only 68 wallets qualify; scoring all ~18k holders needs the server.

### Data fix + playbook tests (2026-11-14, later)
- `/v2/resolutions` returns `resolved` with `payouts: null` for 45,183 of 208k markets (neg-risk ladders: weather,
  tweet counts, price levels): 428k trades (17%) were silently dropped. Backfilled from Gamma final prices
  (`fetch_activity.backfill_null_payouts`). Re-run: 1.69M resolved trades, 109.6k events, bias check +0.78c.
- Main model unchanged in substance: PnL skill predictive (+0.16), not tradable (-2.4c net).
- Playbook claims (prompts/00_desk_charter.md evidence table): specialization raises persistence (+0.25; esports +0.32)
  but no selection clears copy costs; $400 copy medians $76-$232; case-study selection 28% profitable, mean carried by
  one wallet; win rate useless (+0.05). Crypto Up/Down: 15m/4h = Chainlink 60s TWAP, daily = Binance 1m close.
