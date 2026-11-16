# Prompts

One shared charter plus one prompt per study. Each study lives in its own folder under `studies/`, with its prompt,
code and outputs, and is a job in `pipeline/jobs.yaml`. **When a study prompt conflicts with the charter, the
charter's method and data rules win.**

| Prompt | Study folder | Pipeline jobs | Dashboard |
|---|---|---|---|
| [`00_desk_charter.md`](00_desk_charter.md) | (all) | (all) | |
| [`studies/holder_quality_scan/PROMPT.md`](../studies/holder_quality_scan/PROMPT.md) | `studies/holder_quality_scan/` | `holder-scan` (daily) | Holder Quality Scan |
| [`studies/central_bank/PROMPT.md`](../studies/central_bank/PROMPT.md) | `studies/central_bank/` | `cb-recorder` (always on), `cb-daily`, `cb-weekly` | FOMC Market Making |
| Playbook evidence (charter appendix) | `scripts/test_kb_hypotheses.py` | not scheduled (one-off study) | |

Adding a study: create `studies/<name>/PROMPT.md` (header: version, "load with the charter", scope), its code, an
`art/` export of `{rows, meta}` JSON files, and a job in `pipeline/jobs.yaml`. Reference charter sections instead of
repeating them.

## Where each source prompt went
| Source | Now in |
|---|---|
| Master desk prompt (pasted twice; deduplicated) | `00_desk_charter.md` §1, §3-§8, §10-§12 |
| Operating prompt v2 (formerly `docs/OPERATING_PROMPT.md`) | charter §2, §3 settlement, §6 integrity gate, §9, §10 write-up, appendix |
| Holder-quality scan v3 | `studies/holder_quality_scan/PROMPT.md` (v4) |
| Central Bank FOMC study | `studies/central_bank/PROMPT.md` |

## Conflicts resolved
| Topic | Conflict | Resolution |
|---|---|---|
| Crypto settlement | Scan v3: "averaged over last 30-60 s" | Verified: 60 s TWAP on 15-min/4-h; daily = single 1-min candle |
| Replication data | `/v2/positions` returns rows sorted by PnL, so a capped read keeps only winners | Read with `sort_by=TIMESTAMP`; coverage check against `/v2/activity`; excess-win check |
| Replication hindsight | Scan tags wallets on all-time stats, then tests the last 30 days | Also score as of `now-30d` (point in time) and report both sets |
| Fresh-wallet longshots | Scan treats as a signal | Kept as an alert, marked untested on our data |
