# Central Bank: FOMC market-making study

Prompt: [`PROMPT.md`](PROMPT.md). Paper only.

| File | Phase | Does |
|---|---|---|
| `discover.py` | setup | resolves the Dec 2026, Jan 2027 and Mar 2027 "Fed Decision" events to token ids, fees and reward terms → `out/markets.json` |
| `recorder.py`, `run_recorder.sh` | 1 | books (top 5 levels) every 5 s via `POST /books`, trades from the market websocket, gaps and reconnects → `data/central_bank/raw/` |
| `signals.py` | 2 | signal vs mid change at 30 s/1 m/5 m/15 m per meeting, news/price-bucket splits, spoofing test, curve check |
| `paper.py` | 3 | five placeholder quoting strategies through the fill model (until `pm_mm_backtest.py` arrives) |
| `report.py` | report | dashboard data in `art/`, backtester-schema CSVs in `data/central_bank/csv/` |

## Open inputs
| Input | Effect while missing |
|---|---|
| `pm_mm_backtest.py` | strategies P1-P5 are placeholders |
| ZQ futures source | `anchor` and `anchor_minus_mid` are null; curve gap vs ZQ is null |
| US data calendar | `news` is null; no news/quiet split |
| Kalshi access | the network proxy blocks the Kalshi API from the cloud session; no cross-venue data |

## Verified on 16 Nov 2026
- 15 outcomes (5 per meeting: 50+ bp cut, 25 bp cut, no change, 25 bp hike, 50+ bp hike), neg-risk.
- Fees: rate 0.05, taker only, maker rebate 25% of taker fees.
- Liquidity rewards on 6 outcomes (e.g. Dec 25 bp hike $1,000/day, max spread 3.5 c, min size 200).
