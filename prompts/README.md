# Prompts

One shared charter, plus one module per task. To run a task, load the charter and that task's module.
**When they conflict, the charter's method and data rules win.**

| File | What it is | When to use |
|---|---|---|
| [`00_desk_charter.md`](00_desk_charter.md) | Role, principles, universe, research streams, smart-money definitions, infrastructure, data/method rules, workflows, output style, standing tasks, evidence table | Always |
| [`10_holder_quality_scan.md`](10_holder_quality_scan.md) | Daily scan: grade top holders, insider-like / smart tags, replication test, watch-list | "What's new", daily scan |
| [`20_central_bank_mm_study.md`](20_central_bank_mm_study.md) | FOMC market-making study: recorder, signal study, paper trading, weekly report (paper only) | Fed-decision markets, MM research |

Adding a task: copy the header of an existing module (version, "load with charter", scope), keep only task-specific
rules, and reference charter sections instead of repeating them.

## Where each source prompt went
| Source | Now in |
|---|---|
| Master desk prompt (pasted twice; deduplicated) | `00_desk_charter.md` §1, §3-§8, §10-§12 |
| Operating prompt v2 (`docs/OPERATING_PROMPT.md`) | charter §2, §3 settlement, §6 integrity gate, §9, §10 write-up, appendix |
| Holder-quality scan v3 | `10_holder_quality_scan.md` (v4) |
| Central Bank FOMC study | `20_central_bank_mm_study.md` |

## Conflicts resolved
| Topic | Conflict | Resolution |
|---|---|---|
| Crypto settlement | Scan v3: "averaged over last 30-60 s" | Verified: 60 s TWAP on 15-min/4-h; daily = single 1-min candle |
| Replication data | Scan uses `/v2/positions`, which the v2 tests found winner-biased | Keep endpoint, add the 2c excess-win check and an activity/resolutions fallback |
| Replication hindsight | Scan tags wallets on all-time stats, then tests the last 30 days | Score as of `now-30d` where possible, else label "in-sample upper bound" |
| Fresh-wallet longshots | Scan treats as a signal | Kept as an alert, marked untested on our data |
