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

The pipeline indexes ExchangeV3 and PositionManager, selects asset ids by `version`, and decodes all V2 lifecycle events (below).

## Lifecycle & resolution events (CTF + V2): verified 2026-11-14

Registry: `src/pmkt/abi/events.json` (137 events), built by `scripts/build_event_registry.py` from
`github.com/Polymarket/polymarket-v2-external` @ `741f8bb` (2026-09-24): compiled ABIs for CTF/NegRiskAdapter/UMA,
parsed Solidity for V2 (UDVTs: `ConditionId=bytes31`, `EventId=bytes29`, `PositionId=uint256`).
**All 49 topic0s seen live on the 11 Polymarket contracts match a registry signature** (45 lifecycle + 4 ERC-1155/exchange
handled elsewhere), so the signatures are confirmed on-chain, not just taken from source. No same-signature/different-indexing ambiguity.

| Check (live data) | Result |
|---|---|
| `PositionMigrated.positionId` bit-decodes to the indexed `conditionId` + `outcomeIndex` | 115/115 (40k blocks; binary 103, negrisk 12) |
| `PositionMigrated.amount` == PositionManager mint to same holder/positionId in same tx | 60/60 |
| PositionManager token ids that are valid V2 position ids | 1,011/1,011 |
| CTF token ids misclassified as V2 by `is_v2_position_id` | 0/12,621 |
| ExchangeV3 fills on V2 ids; CTF exchanges on CTF ids | 1,833/1,833; 0/136,501 |
| `MigrationResolved.result` == normalised CTF payout (ppm) | 123/123 |
| ExchangeV3 taker fills vs Data API (price & size) | 38/40 match; the 2 others are combo *mint matches* (BUY vs BUY on complements, prices sum to 1) that the API logs as `SPLIT` with no price. The chain is the more complete record |

Notes:
- Today all V2 *trading* is **Combos** (CombinatorialModule; taker fee ≈ 247 bps of notional). Binary/neg-risk V2 trading starts 11-23.
- **Migration is already live**: in one hour 1,761 CTF conditions were registered (`MigrationConditionRegistered`); every one that maps to an open market (1,269) is a **v1 sports** market, i.e. pre-registration ahead of the 12-01 cutover. `lifecycle.condition_map()` gives the authoritative CTF→V2 condition link.
- CTF `PositionSplit` is dominated by the exchanges themselves as `stakeholder` (mint matches), not user splits.
- Router and AutoRedeemer events are **context only** (`is_context_only=True`); balances come from ERC-1155 transfers. Never double count.
- Neg-risk derivation: `RemainingConditionsDerivableAsNo` / `SyntheticConditionDerivableAsYes` must be applied instead of waiting for one `ConditionResolved` per outcome; never derive sibling-NO for migrated conditions (docs).
