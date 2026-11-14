# Data sources: verified facts

Everything below was checked against docs.polymarket.com **and** live responses on **2026-11-14**.
Re-verify before relying on it. Polymarket Protocol V2 starts rolling out on **2026-11-23**.

## Endpoints

| Source | URL | Notes |
|---|---|---|
| Gamma (metadata) | `https://gamma-api.polymarket.com` | `/markets` offset capped at 10k; deep paging via `/markets/keyset`, `/events/keyset` with `after_cursor` (100/page). `order=volume24hr` works on `/markets`. `tag_slug` works on `/events/keyset`, is **ignored** on `/markets/keyset`. `volume_24hr_min` is ignored. Batch `GET /events?id=a&id=b`. |
| CLOB REST | `https://clob.polymarket.com` | `/book?token_id=` used for recorder validation. |
| CLOB WS market | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Subscribe `{"assets_ids":[...],"type":"market","custom_feature_enabled":true}`; add/remove with `"operation":"subscribe"/"unsubscribe"`. Text `PING` every 10 s. Events: `book`, `price_change` (size = new absolute level size), `last_trade_price`, `tick_size_change`, `best_bid_ask`, `new_market`, `market_resolved`. |
| Data API | `https://data-api.polymarket.com/v2` | **v1 retired 2026-11-02 (HTTP 410).** Envelope `{data, pagination{next_cursor,has_more}}`, snake_case, `condition` ≤ 20 ids, `offset`/`market` params → 400. `/v2/status` reports indexer lag. |
| Polygon RPC | `https://polygon-bor-rpc.publicnode.com` (public) | `eth_getLogs` returns `blockTimestamp`. ~39k fills/500 blocks in ~4 s. Use a keyed provider in prod. |

### Undocumented / surprising behaviour found
- `/v2/holders`: OpenAPI says `limit ≤ 1000`, server enforces **`limit ≤ 100` when `include_pnl=true`**. Cursor pagination works beyond 100.
- `/v2/holders` reports `avg_price = 0` for positions obtained by split/convert (e.g. one neg-risk minter holds ~49M NO shares across 23 longshot markets). Treat as **entry unknown**, not zero cost.
- Gamma open-events crawl returns ~357k markets, 134k of them already closed (sub-markets of open events). Full snapshot with raw JSON = **349 MB**; slim open-only = 57 MB.

## On-chain (Polygon, chain 137)

| Contract | Address | Role |
|---|---|---|
| CTF Exchange | `0xE111180000d2663C0091e4f400237545B87B996B` | fills (v1 binary) |
| Neg Risk CTF Exchange | `0xe2222d279d744050d28e00520010520000310F59` | fills (v1 neg-risk) |
| ExchangeV3 (Poly v2) | `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` | fills (v2) |
| Conditional Tokens | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | ERC-1155 ledger v1 |
| PositionManager (Poly v2) | `0x006F54F7f9A22e0000CC2AB60031000000ae9fEF` | ERC-1155 ledger v2 |
| pUSD collateral | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | collateral (6 dp) |

`OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)`
topic0 `0xd543adfd…d8ee`, identical on all three exchanges.

Semantics (verified vs `/v2/trades` on the same tx hashes):
- One row per **order** filled; `maker` = order owner. The taker's own order is the row whose `taker` field = exchange address.
- `side 0 = BUY`: makerAmount = collateral paid, takerAmount = shares. `side 1 = SELL`: reverse.
- Maker rows can carry the **complementary token** (mint/merge matches).
- `fee` on taker rows = protocol taker fee. Fees on maker rows occur **only with a builder code** (flat 5/25/50 bps of notional) = third-party app fees.

Gamma `version` field: `"v1"` → CTF `clobTokenIds`; `"v2"` → PositionManager `positionIds`. As of 2026-11-14 all open markets are v1.

## Fees (docs.polymarket.com/trading/fees, 2026-11-14)
`fee = shares × rate × p × (1−p)`, takers only. Rates: crypto 0.07, sports 0.05, economics/culture/weather/other 0.05, finance/politics/mentions/tech 0.04, **geopolitics 0**, combos 0.05. Maker rebates 15–25% of fees by category.
Observed 1h (13:03–13:48 UTC 2026-11-14): taker fees averaged **165 bps of notional** (no builder) and 247 bps with a builder.
Per-market truth: Gamma `feeSchedule {rate, exponent, takerOnly, rebateRate}`.

## Protocol V2 rollout (docs migrate/polymarket-v2)
| Phase | Start (ET) | Markets |
|---|---|---|
| 1 | Mon 2026-11-23 08:30 | BNB/DOGE/HYPE/SOL/XRP/ZEC Up/Down 5m & 15m; some weather |
| 2 | Tue 2026-11-24 | BTC/ETH Up/Down 5m/15m/4h; other crypto, weather, commodities, finance, minor sports |
| 3 | Tue 2026-12-01 | Soccer, NFL/CFB, basketball, tennis, esports |

The pipeline already indexes ExchangeV3 and PositionManager and selects asset ids by `version`. **To do before 11-23:** decode V2 lifecycle events (split/merge/redeem/migration) and verify that V2 fills price/size identically.
