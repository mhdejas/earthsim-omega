#!/usr/bin/env python3
"""Event-driven scheduler for the experimental forecast cycle.

Polls Open-Meteo's ECMWF IFS 0.25 deg ensemble metadata every ``--poll-min`` minutes. When a new model
run (``last_run_initialisation_time``) is available, it runs one full cycle (``earthsim forecast run``)
under an exclusive file lock, so overlapping cycles cannot happen. Failed cycles are retried on the
next poll (at most ``--max-retries`` times per model run); old run folders beyond ``--keep`` are removed.
State is kept in data/forecast/scheduler_state.json and every decision is appended to
logs/forecast_scheduler.log.

Usage:  python scripts/forecast_scheduler.py [--once] [--poll-min 10] [--keep 60]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META = "https://ensemble-api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json"
STATE = ROOT / "data" / "forecast" / "scheduler_state.json"
LOG = ROOT / "logs" / "forecast_scheduler.log"
LOCK = ROOT / "data" / "forecast" / ".cycle.lock"


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def latest_init() -> int | None:
    try:
        req = urllib.request.Request(META, headers={"User-Agent": "EARTHSIM-Omega/0.2 scheduler"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(json.loads(r.read())["last_run_initialisation_time"])
    except Exception as e:
        log(f"meta poll failed: {e}")
        return None


def prune(keep: int) -> None:
    runs = ROOT / "data" / "forecast" / "runs"
    if not runs.exists():
        return
    dirs = sorted(p for p in runs.iterdir() if p.is_dir() and not p.is_symlink())
    latest = (runs / "latest").resolve() if (runs / "latest").exists() else None
    for p in dirs[:-keep] if len(dirs) > keep else []:
        if p.resolve() != latest:
            shutil.rmtree(p)
            log(f"pruned {p.name}")


def cycle(workers: int) -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another cycle holds the lock; skipping")
            return False
        exe = Path(sys.executable).parent / "earthsim"
        cmd = [str(exe) if exe.exists() else "earthsim", "forecast", "run", "--workers", str(workers)]
        log("start cycle: " + " ".join(cmd[1:]))
        t = time.time()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
        log(f"cycle exit={r.returncode} in {time.time() - t:.0f}s {tail[0][:300]}")
        return r.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once, run if a new model run exists, then exit")
    ap.add_argument("--poll-min", type=float, default=10)
    ap.add_argument("--keep", type=int, default=60)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-retries", type=int, default=3)
    a = ap.parse_args()
    st = json.loads(STATE.read_text()) if STATE.exists() else {"done_init": None, "attempts": {}}
    log(f"scheduler up (poll {a.poll_min} min, keep {a.keep}); last processed init={st['done_init']}")
    while True:
        init = latest_init()
        if init is not None and init != st["done_init"]:
            k = str(init)
            n = st["attempts"].get(k, 0)
            if n < a.max_retries:
                log(f"new ECMWF ensemble run init={time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(init))} (attempt {n + 1})")
                st["attempts"][k] = n + 1
                if cycle(a.workers):
                    st["done_init"] = init
                    st["attempts"] = {}
                    prune(a.keep)
                STATE.parent.mkdir(parents=True, exist_ok=True)
                STATE.write_text(json.dumps(st, indent=2))
        if a.once:
            break
        time.sleep(a.poll_min * 60)


if __name__ == "__main__":
    main()
