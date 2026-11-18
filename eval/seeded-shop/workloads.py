#!/usr/bin/env python3
"""Run seeded-shop workloads through the tracing stack and build one trace per run.

    workloads.py [--repeat N] [--only NAME ...]

Requires a running stack (webkit/stack/wkstack.py up --config eval/seeded-shop/stack.toml).
Each run reseeds the database, runs one scenario, and writes
runs/<run-id>/traces/<scenario>-<n>.ndjson via webkit/tracebuilder.

Scenarios cover every endpoint and the concurrent interactions the model must reproduce:
sequential mixes (including error paths) and races between overlapping requests. These are
workloads for trace validation (step 5), not bug reproductions: they record whatever the
app does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "webkit" / "tracebuilder"))
import build  # noqa: E402

STATE = REPO / "runs" / ".current-seeded-shop.json"
MAPPING = Path(__file__).resolve().parent / "mapping.toml"
BASE = "http://localhost:8080"


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         idem: str | None = None) -> int:
    headers = {"content-type": "application/json"} if body is not None else {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if idem:
        headers["idempotency-key"] = idem
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def together(*calls) -> list[int]:
    """Start the calls at the same moment (barrier) and wait for all."""
    barrier = threading.Barrier(len(calls))
    out: list[int] = [0] * len(calls)

    def run(i: int) -> None:
        barrier.wait()
        out[i] = call(*calls[i])

    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def checkout(token, product, qty, coupon=None, idem=None):
    body = {"productId": product, "quantity": qty}
    if coupon:
        body["couponCode"] = coupon
    return ("POST", "/checkout", token, body, idem)


SCENARIOS = {
    # Every endpoint once, one at a time, including error paths.
    "sequential": lambda: [
        call("GET", "/health"),
        call("GET", "/products/1"), call("GET", "/products/9"),
        call(*checkout("tok-bob", 2, 2, idem="s-1")), call(*checkout("tok-bob", 2, 2, idem="s-1")),
        call(*checkout("tok-alice", 2, 1, coupon="WELCOME10")), call(*checkout("tok-alice", 2, 1, coupon="WELCOME10")),
        call(*checkout("tok-carol", 2, 1, coupon="NOPE")), call(*checkout("tok-carol", 9, 1)),
        call(*checkout("tok-carol", 1, 3)), call(*checkout("nope", 2, 1)), call("POST", "/checkout", None, {"productId": 2, "quantity": 1}),
        call("GET", "/orders", "tok-bob"), call("POST", "/orders/1/cancel", "tok-bob"),
        call("POST", "/orders/1/cancel", "tok-bob"), call("POST", "/orders/2/cancel", "tok-bob"),
        call("POST", "/products/2/restock", "tok-alice", {"quantity": 3}),
        call("POST", "/products/9/restock", "tok-alice", {"quantity": 1}),
        call("GET", "/products/2"),
    ],
    # Two concurrent restocks of one product (the row lock makes the second wait).
    "restock_race": lambda: together(("POST", "/products/2/restock", "tok-alice", {"quantity": 1}),
                                     ("POST", "/products/2/restock", "tok-carol", {"quantity": 2})),
    # Two buyers of the last item.
    "last_item": lambda: together(checkout("tok-alice", 1, 1), checkout("tok-carol", 1, 1)),
    # A client retry overlapping the original request, same Idempotency-Key.
    "retry_same_key": lambda: together(checkout("tok-bob", 2, 1, idem="r-1"), checkout("tok-bob", 2, 1, idem="r-1")),
    # Three concurrent redemptions of one coupon by one user.
    "coupon_race": lambda: together(*[checkout("tok-bob", 2, 1, coupon="WELCOME10")] * 3),
    # Concurrent cancels of the same order, after placing it.
    "cancel_race": lambda: [call(*checkout("tok-bob", 2, 3, coupon="WELCOME10"))]
                           + together(("POST", "/orders/1/cancel", "tok-bob"), ("POST", "/orders/1/cancel", "tok-bob")),
    # Eight mixed requests at once on a pool of two connections.
    "mixed": lambda: together(
        checkout("tok-alice", 1, 1), checkout("tok-carol", 1, 1),
        checkout("tok-bob", 2, 1, idem="m-1"), checkout("tok-bob", 2, 1, idem="m-1"),
        checkout("tok-alice", 2, 2, coupon="WELCOME10"), checkout("tok-carol", 2, 1, coupon="WELCOME10"),
        ("GET", "/products/1"), ("GET", "/orders", "tok-bob")),
}


def reseed() -> None:
    cid = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=seeded-shop",
                          "--filter", "label=com.docker.compose.service=app"],
                         check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["docker", "exec", cid, "npm", "run", "-s", "db:seed"], check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    if not STATE.exists():
        sys.exit("no running stack; start it with wkstack.py up")
    run = Path(json.loads(STATE.read_text())["out_dir"])
    mapping = build.Mapping(MAPPING)

    for name, scenario in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        for n in range(1, args.repeat + 1):
            reseed()
            time.sleep(0.5)
            since = time.time_ns()
            statuses = scenario()
            time.sleep(1.0)            # let the recorders flush
            until = time.time_ns()
            out = run / "traces" / f"{name}-{n}.ndjson"
            lines = build.build(run, mapping, since, until)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(json.dumps(x) + "\n" for x in lines))
            windows_file = run / "traces" / "windows.json"
            windows = json.loads(windows_file.read_text()) if windows_file.exists() else {}
            windows[out.stem] = [since, until]           # lets a different mapping rebuild the same run
            windows_file.write_text(json.dumps(windows, indent=1))
            print(f"{out.name}: {len(lines) - 1} events, statuses {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
