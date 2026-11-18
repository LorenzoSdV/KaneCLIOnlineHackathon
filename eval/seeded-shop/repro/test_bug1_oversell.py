#!/usr/bin/env python3
"""Reproduction of model-checking finding B1 (NoNegativeStock) on the real seeded-shop app.

    test_bug1_oversell.py [--runs N]

This is the kind of `repro/test_bug<N>_*` file a Phase-4 agent writes, using the generic
webkit/repro library. Requires the tracing stack (wkstack.py up --config eval/seeded-shop/stack.toml).

Escalation level: 1 (timing assistance). The app is used only through its public HTTP API, with
real seeded users; no database writes, no internal calls, no code changes. The pgproxy gate
releases the two requests' SQL statements in the order of the TLC counterexample.

For each run it checks:
  1. consequence through the public API: both buyers of the last item get 201, and
     GET /products/1 shows stock -1;
  2. same bug: the run's own recorded trace is reproduced by the model AND ends in a state
     violating NoNegativeStock (webkit/tracecheck --violates).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
REPO = APP.parents[1]
sys.path.insert(0, str(REPO / "webkit" / "repro"))
sys.path.insert(0, str(REPO / "webkit" / "tracebuilder"))
import build  # noqa: E402
import scheduler  # noqa: E402

BASE = "http://localhost:8080"
STATE = REPO / "runs" / ".current-seeded-shop.json"


def http(method: str, path: str, token: str = "", body: dict | None = None, traceparent: str = "",
         idem: str = "") -> tuple[int, dict]:
    headers = {}
    if body is not None:
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    if idem:
        headers["idempotency-key"] = idem
    if traceparent:
        headers["traceparent"] = traceparent
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def send_for(schedule: dict):
    """Build each actor's HTTP request from its counterexample inputs."""
    def send(actor: str, traceparent: str):
        i = schedule["inputs"][actor]["input"]
        assert i["route"] == "Checkout", i
        body = {"productId": i["productId"], "quantity": i["quantity"]}
        if i["couponCode"]:
            body["couponCode"] = i["couponCode"]
        return http("POST", "/checkout", i["token"], body, traceparent, i["idem"])[0]
    return send


def reseed() -> None:
    cid = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=seeded-shop",
                          "--filter", "label=com.docker.compose.service=app"],
                         check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["docker", "exec", cid, "npm", "run", "-s", "db:seed"], check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    if not STATE.exists():
        sys.exit("no running stack; start it with wkstack.py up")
    run_dir = Path(json.loads(STATE.read_text())["out_dir"])
    out = run_dir / "repro" / "bug1"
    out.mkdir(parents=True, exist_ok=True)

    schedule_path = out / "schedule.json"
    subprocess.run([sys.executable, str(REPO / "webkit" / "repro" / "schedule_from_tlc.py"),
                    "--spec", str(APP / "spec" / "MC.tla"), "--config", str(APP / "spec" / "MC_hunt_last_item.cfg"),
                    "--invariant", "NoNegativeStock", "--out", str(schedule_path)], check=True,
                   stdout=subprocess.DEVNULL)
    schedule = json.loads(schedule_path.read_text())
    mapping = build.Mapping(APP / "mapping.toml")

    reproduced = 0
    for n in range(1, args.runs + 1):
        reseed()
        time.sleep(0.5)
        since = time.time_ns()
        res = scheduler.replay(schedule, send_for(schedule))
        status, product = http("GET", "/products/1")
        time.sleep(1.0)                               # let the recorders flush
        until = time.time_ns()

        trace = out / f"run-{n}.ndjson"
        trace.write_text("".join(json.dumps(x) + "\n" for x in build.build(run_dir, mapping, since, until)))
        check = subprocess.run(
            [sys.executable, str(REPO / "webkit" / "tracecheck" / "check.py"),
             "--spec", str(APP / "spec" / "TraceShop.tla"), "--template", str(APP / "spec" / "TraceShop.cfg.tmpl"),
             "--violates", "NoNegativeStock", "--invariants-module", "ShopInvariants", str(trace)],
            capture_output=True, text=True)
        same_bug = check.returncode == 0

        statuses = [res.responses[a] for a in schedule["actors"]]
        consequence = statuses == [201, 201] and product.get("stock") == -1
        ok = consequence and same_bug
        reproduced += ok
        waits = [f"{s.actor}#{s.n}" for s in res.steps if s.outcome == "lock-wait"]
        print(f"run {n}: {'REPRODUCED' if ok else 'NOT REPRODUCED'} - checkout statuses {statuses}, "
              f"GET /products/1 stock {product.get('stock')}, lock waits at {waits}, "
              f"trace check: {'same invariant violated' if same_bug else check.stdout.strip()[-200:]}")

    print(f"\nB1 reproduced in {reproduced}/{args.runs} runs (Level 1: proxy-scheduled, public API only)")
    return 0 if reproduced == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
