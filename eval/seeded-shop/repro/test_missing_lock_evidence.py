#!/usr/bin/env python3
"""L3 path without an agent: a model that misses a FOR UPDATE yields LockBlocked on replay.

    test_missing_lock_evidence.py SPEC_DIR

SPEC_DIR holds a Shop model whose restock reads the product with a plain SELECT (missing the
FOR UPDATE that src/server.ts takes). Its counterexample to RestocksAllCount (a lost restock)
is replayed against the real app. The app's FOR UPDATE makes the second restock's read wait
on the first one's lock, which the schedule says cannot happen, so the replay must stop with
LockBlocked naming the other request: the evidence for a SPEC_REPAIR.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "webkit" / "repro"))
import scheduler  # noqa: E402
import test_bug1_oversell as b1  # noqa: E402


def send_for(schedule: dict):
    def send(actor: str, traceparent: str):
        i = schedule["inputs"][actor]["input"]
        assert i["route"] == "Restock", i
        return b1.http("POST", f"/products/{i['productId']}/restock", i["token"],
                       {"quantity": i["quantity"]}, traceparent)[0]
    return send


def main() -> int:
    spec = Path(sys.argv[1]).resolve()
    run_dir = Path(json.loads(b1.STATE.read_text())["out_dir"])
    sched_path = run_dir / "repro" / "missing-lock" / "schedule.json"
    subprocess.run([sys.executable, str(REPO / "webkit" / "repro" / "schedule_from_tlc.py"),
                    "--spec", str(spec / "MC.tla"), "--config", str(spec / "MC_hunt_restock.cfg"),
                    "--invariant", "RestocksAllCount", "--out", str(sched_path)], check=True,
                   stdout=subprocess.DEVNULL)
    schedule = json.loads(sched_path.read_text())
    b1.reseed()
    try:
        res = scheduler.replay(schedule, send_for(schedule), step_timeout=5)
    except scheduler.LockBlocked as e:
        ok = len(e.blocked_by) == 1 and e.blocked_by[0] != e.actor
        print(f"{'PASS' if ok else 'FAIL'}: LockBlocked: {e}")
        return 0 if ok else 1
    print(f"FAIL: replay completed without lock-wait evidence; responses {res.responses}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
