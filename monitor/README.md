# Polymarket Monitor

Dashboard page (claude.ai Data artifact) fed by one JSON export.

1. `python scripts/export_monitor.py` builds `data/monitor/monitor.json` from the Parquet lake and `data/recon/`.
2. The JSON is attached to the artifact as a file; each dataset picks one table from it with a loader.
3. `index.html` is the page stored in the artifact (kept here for review and history).

Refresh = re-run the export and replace the attached file. Not automated yet: once the stack runs on the
always-on host, schedule the export and upload there.
