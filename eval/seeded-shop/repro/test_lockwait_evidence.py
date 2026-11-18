#!/usr/bin/env python3
"""Check that an impossible schedule yields lock-wait evidence (webkit/repro LockBlocked).

    test_lockwait_evidence.py

Takes B1's real reproduction schedule and swaps the final two COMMITs, so the schedule asks r2
to commit while r2's UPDATE is still waiting on the row lock r1 holds. That is what a
counterexample from a model with a missing lock would ask for. The replay must stop with
LockBlocked naming r1, which step 7 turns into a citable SPEC_REPAIR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "webkit" / "repro"))
import scheduler  # noqa: E402
import test_bug1_oversell as b1  # noqa: E402


def main() -> int:
    run_dir = Path(json.loads(b1.STATE.read_text())["out_dir"])
    sched_path = run_dir / "repro" / "bug1" / "schedule.json"
    if not sched_path.exists():
        sys.exit("run test_bug1_oversell.py first (it writes the schedule)")
    schedule = json.loads(sched_path.read_text())
    order = schedule["order"]
    assert [e["actor"] for e in order[-2:]] == ["r1", "r2"], "expected r1 then r2 COMMIT at the end"
    schedule["order"] = order[:-2] + [order[-1], order[-2]]   # ask r2 to commit first

    b1.reseed()
    try:
        scheduler.replay(schedule, b1.send_for(schedule), step_timeout=5)
    except scheduler.LockBlocked as e:
        ok = e.actor == "r2" and e.blocked_by == ["r1"]
        print(f"{'PASS' if ok else 'FAIL'}: LockBlocked: {e}")
        return 0 if ok else 1
    print("FAIL: the impossible schedule replayed without a lock-wait error")
    return 1


if __name__ == "__main__":
    sys.exit(main())
