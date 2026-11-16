# Holder-quality scan

Prompt: [`PROMPT.md`](PROMPT.md). Run: `python3 scan.py && python3 enrich.py` (or `python3 -m pipeline.run run holder-scan`).

| File | Does |
|---|---|
| `hq_common.py` | HTTP pool (8 parallel, retries), categories, wallet record features, SMART / INSIDER-LIKE scoring |
| `scan.py` | markets by category and expiry, top-20 holders per side, wallet records, tags (now and as of 30 days ago) → `out/scan.json` |
| `enrich.py` | portfolio exposure, 7-day prices, 72-hour flow by group, 90-day class PnL curves, replication test, signals → `out/enriched.json`, `art/*.json` |

Notes from the first run (16 Nov 2026):
- `/v2/positions` returns settled rows sorted by PnL by default, so a capped read keeps only winners. The replication
  test reads with `sort_by=TIMESTAMP`.
- `status=REDEEMABLE` and `REDEEMABLE_LOST` return the same rows; both are read and deduplicated by token.
- `/v2/user-pnl` points are cumulative; daily PnL is the difference.
