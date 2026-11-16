# Study pipeline

Each prompt in `studies/<name>/PROMPT.md` is turned into jobs in [`jobs.yaml`](jobs.yaml). The runner executes the
steps, records each run and archives what it produced.

```
prompt (PROMPT.md + prompts/00_desk_charter.md)
  -> code (studies/<name>/*.py)
  -> raw data (data/<study>/raw/... Parquet, or API snapshots)
  -> outputs (studies/<name>/out/*.json, art/*.json as {rows, meta})
  -> run record (data/pipeline/runs.jsonl) + archive (data/pipeline/archive/<job>/<run>/ with manifest.json)
  -> dashboard (claude.ai artifact; one dataset per art/*.json file)
```

| Job | When (UTC) | What |
|---|---|---|
| `holder-scan` | daily 06:17 | `scan.py` then `enrich.py`: markets, holders, tags, exposure, flow, replication test, signals |
| `cb-recorder` | always on (service) | FOMC books every 5 s + trades; restarts itself, re-resolves markets daily, stops after 17 Mar 2027 |
| `cb-daily` | daily 21:41 | re-resolve token ids, curve check, refresh report data |
| `cb-weekly` | Mondays 07:23 | Phase 2 signal study, Phase 3 paper trading, weekly report data |

## Commands
```bash
python3 -m pipeline.run list            # jobs and schedules
python3 -m pipeline.run run holder-scan # one job now
python3 -m pipeline.run status          # last runs
python3 -m pipeline.run schedule        # scheduler loop (what the docker service runs)
```

## Always-on deployment
```bash
docker compose up -d --build cb-recorder pipeline
```
The recorder must run somewhere persistent: a cloud session container is reclaimed when idle, so the FOMC archive
only stays continuous on a VPS (or any always-on host) running these two services.

## Publishing to the dashboards
Each run writes `art/manifest.json` (file, sha256, row count, dashboard URL). Uploading needs the claude.ai session,
so publishing is done by Claude: ask "publish the latest holder scan", or set up a scheduled Claude task that runs
the job and uploads the files in the manifest to the dashboard's datasets.

## Rules carried from the charter
Paper only, no keys. Failed calls are logged and never filled. Outputs keep the code that made them (`meta` of the
JSON files), and every run records the git revision.
