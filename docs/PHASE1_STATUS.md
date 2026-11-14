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
7. Protocol V2 lifecycle events not decoded yet (needed by 2026-11-23).

## Next steps (in order)
1. Deploy `docker compose up -d` on a VPS (≈ $10–20/mo, 200 GB) **this week**, plus daily sync to object storage.
2. Keyed Polygon RPC (Alchemy/QuickNode) + 90-day fills backfill.
3. Fold transfers + split/merge/redeem into the position book; reconcile vs `/v2/positions` (target > 99% match).
4. V2 event decoding + test on the 11-23 crypto markets.
5. Phase 2: holders/positions dashboard + wallet scoring (all smart-money definitions) on the walk-forward split.
