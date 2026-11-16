#!/usr/bin/env bash
# Keeps the FOMC recorder alive until the last meeting (17 Mar 2027). Restarts on any exit.
# Re-resolves token ids once a day in case markets are added. Paper study: no orders, no keys.
set -u
cd "$(dirname "$0")"
END=$(date -u -d "2027-03-18" +%s)
while [ "$(date -u +%s)" -lt "$END" ]; do
  python3 discover.py || echo "discover failed; keeping previous out/markets.json"
  timeout 24h python3 recorder.py --hours 24
  echo "$(date -u +%FT%TZ) recorder exited ($?); restarting in 10s" >&2
  sleep 10
done
