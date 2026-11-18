#!/usr/bin/env python3
"""Mutation test for trace validation: a wrong model must be rejected at the right action.

    mutants.py TRACE_DIR

Each mutant is a copy of Shop.tla with one realistic modelling mistake. For each mutant the
traces it should conflict with are checked; the mutant passes if trace validation rejects at
least one of them and reports the expected action among the events it could not match.

"expect" names a recorded event: the point where the real execution and the mutant diverge
(e.g. the app sent ROLLBACK where the mutant would continue), not the mutated model step.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CHECK = REPO / "webkit" / "tracecheck" / "check.py"

MUTANTS = [
    {
        "name": "decrement-by-one",
        "why": "models the stock update as stock - 1 instead of stock - quantity",
        "old": "[row EXCEPT !.stock = @ - rq[r].quantity]",
        "new": "[row EXCEPT !.stock = @ - 1]",
        "traces": ["sequential-*"],
        "expect": "Checkout_DecrementStock_finish",
    },
    {
        "name": "missing-unique-constraint",
        "why": "forgets the UNIQUE (couponId, userId) index on CouponRedemption",
        "old": '<< {"orderId"}, {"couponId", "userId"} >>',
        "new": '<< {"orderId"} >>',
        "traces": ["coupon_race-*", "sequential-*"],
        "expect": "Checkout_InsertRedemption_finish",
    },
    {
        "name": "idempotency-check-skipped",
        "why": "models checkout without the Idempotency-Key lookup",
        "old": 'Auth(r, IF rq[r].idem # "" THEN "co_idem" ELSE "co_begin")',
        "new": 'Auth(r, "co_begin")',
        "traces": ["retry_same_key-*", "sequential-*"],
        "expect": "Checkout_FindByKey",
    },
    {
        "name": "cancel-always-succeeds",
        "why": "models cancel as succeeding even when no PENDING order matched",
        "old": 'IF Rows(r) = {} THEN ToRollback(r, "ca_rollback", 409) ELSE Go(r, "ca_read") /\\ UNCHANGED rs',
        "new": 'Go(r, "ca_read") /\\ UNCHANGED rs',
        "traces": ["sequential-*", "cancel_race-*"],
        "expect": "Rollback",   # the app rolled back the 0-row cancel; the mutant would read the order
    },
    {
        "name": "repeatable-read",
        "why": "assumes the checkout transaction runs at REPEATABLE READ instead of READ COMMITTED",
        "old": 'Begin(r, "RC")\n    /\\ Go(r, "co_product")',
        "new": 'Begin(r, "RR")\n    /\\ Go(r, "co_product")',
        "traces": ["last_item-*", "mixed-*", "retry_same_key-*"],
        "expect": "Checkout_DecrementStock_finish",
    },
]


def main() -> int:
    traces_dir = Path(sys.argv[1])
    failures = 0
    for m in MUTANTS:
        traces = sorted({t for pat in m["traces"] for t in traces_dir.glob(pat + ".ndjson")})
        with tempfile.TemporaryDirectory(prefix="wkmut-") as tmp:
            spec = Path(tmp)
            for f in ("TraceShop.tla", "TraceShop.cfg.tmpl"):
                shutil.copy(HERE / f, spec / f)
            src = (HERE / "Shop.tla").read_text()
            assert src.count(m["old"]) == 1, f"{m['name']}: mutation anchor not found exactly once"
            (spec / "Shop.tla").write_text(src.replace(m["old"], m["new"]))
            report = spec / "report.json"
            subprocess.run([sys.executable, str(CHECK), "--spec", str(spec / "TraceShop.tla"),
                            "--template", str(spec / "TraceShop.cfg.tmpl"), "--report", str(report),
                            *map(str, traces)], capture_output=True, text=True)
            results = json.loads(report.read_text())
        rejected = [r for r in results if r["verdict"] == "unmatched"]
        hits = [r for r in rejected if any(s["action"] == m["expect"] for s in r["stuck_at"])]
        ok = bool(hits)
        failures += not ok
        where = "; ".join(f"{Path(r['trace']).name}: stuck at "
                          + ", ".join(s["action"] for s in r["stuck_at"]) for r in rejected) or "none rejected"
        print(f"{'PASS' if ok else 'FAIL'}  {m['name']}: {m['why']}\n"
              f"      {len(rejected)}/{len(results)} traces rejected; expected {m['expect']}; {where}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
