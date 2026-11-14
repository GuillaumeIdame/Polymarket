# pmkt: Polymarket research & trading stack

Data, research, execution and risk stack for directional and market-making strategies on Polymarket.
Currently in **Phase 1** (data foundation). See [`docs/PHASE1_STATUS.md`](docs/PHASE1_STATUS.md).

## Layout
```
config/settings.yaml    endpoints, contracts, universe, fees (all verified; see docs/DATA_SOURCES.md)
src/pmkt/
  gamma.py              market/event snapshots (keyset paging), recorder universe
  recorder.py           CLOB websocket L2/trade archive (multi-conn, PING, reconnect, gap log)
  book.py               L2 book reconstruction from the archive
  chain.py              Polygon OrderFilled + ERC-1155 indexer (resumable cursor, gap detection)
  dataapi.py            Data API v2: holders (+entry/PnL), positions, trades
  positions.py          wallet position book from fills (avg-cost, point-in-time), reconciliation
  holders.py            concentration (top-N, HHI), holder moves (NEW/ADD/REDUCE/EXIT/FLIP)
  fees.py               taker fee model
  storage.py            immutable Parquet lake + DuckDB views + cursors
sql/schema.sql          relational layer design (Postgres)
scripts/validate_book.py  live check: reconstructed books vs REST /book
```
Data lake: `data/raw/{dataset}/dt=YYYY-MM-DD/*.parquet` (git-ignored). Datasets: `gamma_markets`,
`gamma_markets_full`, `recorder_universe`, `ws_market`, `ws_sessions`, `chain_fills`, `chain_transfers`, `api_holders`.

## Quick start
```bash
pip install -e '.[dev]'
pmkt gamma                      # market snapshot
pmkt record --minutes 10        # websocket recorder (0 = forever)
pmkt chain fills --blocks 1800  # last hour of fills, then resumes from cursor
pmkt holders --top 50           # top holders w/ avg entry + PnL
pmkt status                     # inventory, coverage, gaps
pytest -q
```
Always-on deployment: `docker compose up -d --build` (recorder, chain-fills, chain-transfers, holders, gamma).
Set `PMKT_POLYGON_RPC_URL` to a keyed provider for backfills.

## Method rules (enforced in code where possible)
Point-in-time only (`as_of` everywhere, immutable snapshots), count independent events not trades,
model execution costs (fees verified on-chain), separate signal quality from tradability.

Not financial advice.
