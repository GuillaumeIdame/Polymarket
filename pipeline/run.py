"""Study pipeline runner: run a job, run the scheduler, or show status.  Jobs live in pipeline/jobs.yaml.

Every run is recorded in <data>/pipeline/runs.jsonl (job, start, end, status, step exit codes, log path) and its
outputs are archived to <data>/pipeline/archive/<job>/<UTC timestamp>/ with a manifest (file -> sha256, rows),
so any dashboard number can be traced to the run and code version that produced it.

Publishing to the dashboards is a separate step (the manifest lists what to upload), because uploads need the
person's claude.ai session; see pipeline/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("PMKT_DATA_DIR", ROOT / "data")) / "pipeline"
JOBS_FILE = Path(__file__).with_name("jobs.yaml")


def load_jobs() -> dict:
    return yaml.safe_load(JOBS_FILE.read_text())["jobs"]


# ---------------------------------------------------------------- cron (5 fields, UTC): *, n, a-b, a,b, */n, a-b/n
def _field(spec: str, value: int, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def cron_due(expr: str, t: datetime) -> bool:
    mi, hr, dom, mon, dow = expr.split()
    return (_field(mi, t.minute, 0, 59) and _field(hr, t.hour, 0, 23) and _field(dom, t.day, 1, 31)
            and _field(mon, t.month, 1, 12) and _field(dow, (t.isoweekday() % 7), 0, 6))


# ---------------------------------------------------------------- runs
def git_rev() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def manifest(out_dir: Path) -> list[dict]:
    files = []
    for p in sorted(out_dir.glob("*.json")):
        raw = p.read_bytes()
        try:
            rows = len(json.loads(raw).get("rows", []))
        except Exception:  # noqa: BLE001
            rows = None
        files.append({"file": p.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "rows": rows})
    return files


def run_job(name: str, job: dict) -> dict:
    start = datetime.now(timezone.utc)
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    cwd = ROOT / job["cwd"]
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{name}_{stamp}.log"
    rec = {"job": name, "start": start.isoformat(), "git": git_rev(), "prompt": job.get("prompt"), "steps": [],
           "status": "ok", "log": str(log_path)}
    timeout = job.get("timeout_min", 60) * 60
    with log_path.open("w") as log:
        for step in job["steps"]:
            t0 = time.time()
            try:
                p = subprocess.run(step, shell=True, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=None if job.get("service") else timeout)
                code = p.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
            rec["steps"].append({"cmd": step, "exit": code, "seconds": round(time.time() - t0, 1)})
            if code != 0:
                rec["status"] = "failed"
                break
    rec["end"] = datetime.now(timezone.utc).isoformat()
    if rec["status"] == "ok" and job.get("outputs"):
        src = cwd / job["outputs"]
        dst = DATA / "archive" / name / stamp
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.glob("*.json"):
            shutil.copy2(p, dst / p.name)
        man = {"job": name, "run": stamp, "git": rec["git"], "dashboard": job.get("dashboard"),
               "files": manifest(src), "source_dir": str(src.relative_to(ROOT))}
        (dst / "manifest.json").write_text(json.dumps(man, indent=1))
        (src / "manifest.json").write_text(json.dumps(man, indent=1))
        rec["archive"] = str(dst)
    with (DATA / "runs.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps({k: rec[k] for k in ("job", "status", "steps")}, indent=1))
    return rec


def schedule() -> None:
    jobs = load_jobs()
    names = ", ".join(f"{n} [{j['schedule']}]" for n, j in jobs.items() if j.get("schedule"))
    print(f"scheduler: {names}", flush=True)
    last = None
    while True:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if now != last:
            last = now
            for name, job in jobs.items():
                if job.get("schedule") and cron_due(job["schedule"], now):
                    run_job(name, job)
        time.sleep(20)


def status(n: int) -> None:
    f = DATA / "runs.jsonl"
    if not f.exists():
        print("no runs yet")
        return
    for line in f.read_text().splitlines()[-n:]:
        r = json.loads(line)
        took = sum(s["seconds"] for s in r["steps"])
        print(f"{r['start'][:16]}  {r['job']:<12} {r['status']:<7} {took:>7.0f}s  git {r.get('git')}  {r.get('archive', '')}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("job")
    sub.add_parser("schedule")
    s = sub.add_parser("status")
    s.add_argument("-n", type=int, default=20)
    sub.add_parser("list")
    a = ap.parse_args()
    jobs = load_jobs()
    if a.cmd == "run":
        if a.job not in jobs:
            sys.exit(f"unknown job {a.job}; known: {', '.join(jobs)}")
        sys.exit(0 if run_job(a.job, jobs[a.job])["status"] == "ok" else 1)
    if a.cmd == "schedule":
        schedule()
    if a.cmd == "status":
        status(a.n)
    if a.cmd == "list":
        for n, j in jobs.items():
            print(f"{n:<12} {'service' if j.get('service') else j.get('schedule'):<14} {j['prompt']}")


if __name__ == "__main__":
    main()
