# Studies

One folder per study. Each holds its prompt (`PROMPT.md`), its code, small committed outputs (`out/`, `art/`) and is
scheduled through `pipeline/jobs.yaml`. Raw data goes to the git-ignored `data/` lake. Shared rules:
`prompts/00_desk_charter.md`.

| Study | Status | Run | Dashboard |
|---|---|---|---|
| [`holder_quality_scan/`](holder_quality_scan/) | running daily | `python3 -m pipeline.run run holder-scan` | Holder Quality Scan |
| [`central_bank/`](central_bank/) | recorder live since 16 Nov 2026; Phase 2-3 preliminary | `python3 -m pipeline.run run cb-weekly` | FOMC Market Making |
