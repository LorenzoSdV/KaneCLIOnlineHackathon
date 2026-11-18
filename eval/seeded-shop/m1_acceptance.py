#!/usr/bin/env python3
"""M1 acceptance: request attribution and CDC joins under connection pooling.

Requires a running stack:  python3 webkit/stack/wkstack.py up --config eval/seeded-shop/stack.toml

Fires 8 concurrent requests (pool size 2, so requests share connections) and
checks, from the recordings alone:
  1. every SQL statement in the window is attributed to exactly one HTTP request;
  2. each transaction ran on one connection and belongs to one request;
  3. pooled connections really were shared by different requests, and requests'
     statements really interleaved;
  4. every committed CDC transaction joins (by xid) to exactly one proxy
     transaction, and every proxy transaction that wrote has a CDC record;
  5. rows inserted per CDC match the rows the proxied INSERT ... RETURNING saw.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "runs" / ".current-seeded-shop.json"
BASE = "http://localhost:8080"
WRITE_TAGS = ("INSERT", "UPDATE", "DELETE")


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def request(method: str, path: str, token: str | None = None, body: dict | None = None,
            idem: str | None = None) -> int:
    headers = {"content-type": "application/json"}
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


def reseed() -> None:
    cid = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=seeded-shop",
         "--filter", "label=com.docker.compose.service=app"],
        check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["docker", "exec", cid, "npm", "run", "-s", "db:seed"], check=True, capture_output=True)


WORKLOAD = [
    ("POST", "/checkout", "tok-alice", {"productId": 1, "quantity": 1}, None),
    ("POST", "/checkout", "tok-carol", {"productId": 1, "quantity": 1}, None),
    ("POST", "/checkout", "tok-bob", {"productId": 2, "quantity": 1}, "retry-1"),
    ("POST", "/checkout", "tok-bob", {"productId": 2, "quantity": 1}, "retry-1"),
    ("POST", "/checkout", "tok-alice", {"productId": 2, "quantity": 2, "couponCode": "WELCOME10"}, None),
    ("POST", "/checkout", "tok-carol", {"productId": 2, "quantity": 1, "couponCode": "WELCOME10"}, None),
    ("GET", "/products/1", None, None, None),
    ("GET", "/orders", "tok-bob", None, None),
]


def main() -> int:
    if not STATE.exists():
        sys.exit("no running stack; start it with wkstack.py up")
    out = Path(json.loads(STATE.read_text())["out_dir"])

    reseed()
    time.sleep(0.5)
    t0 = time.time_ns()
    barrier = threading.Barrier(len(WORKLOAD))
    statuses: list[int | None] = [None] * len(WORKLOAD)

    def run(i: int) -> None:
        method, path, token, body, idem = WORKLOAD[i]
        barrier.wait()
        statuses[i] = request(method, path, token, body, idem)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(WORKLOAD))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(1.5)  # let recorders flush

    inbound = [e for e in read(out / "inbound.ndjson") if e.get("t_start_ns", 0) >= t0]
    proxy = [e for e in read(out / "pgproxy.ndjson") if e.get("ts_ns", 0) >= t0]
    cdc = [e for e in read(out / "cdc.ndjson") if e.get("ev") == "txn" and e.get("commit_time_ns", 0) >= t0]

    requests = {e["trace_id"]: e for e in inbound if e["ev"] == "response"}
    stmts = [e for e in proxy if e["ev"] == "stmt"]
    txn_ends = [e for e in proxy if e["ev"] == "txn_end"]

    results: list[tuple[str, bool, str]] = []

    # 1. attribution
    unattributed = [s for s in stmts if s.get("trace_id") not in requests]
    results.append(("every statement attributed to an HTTP request",
                     not unattributed and bool(stmts),
                     f"{len(stmts)} statements, {len(requests)} requests, {len(unattributed)} unattributed"))

    # 2. transactions: one connection, one request
    by_txn: dict[str, list[dict]] = defaultdict(list)
    for s in stmts:
        if s.get("txn"):
            by_txn[s["txn"]].append(s)
    bad_txn = [t for t, ss in by_txn.items()
               if len({s["conn"] for s in ss}) != 1 or len({s.get("trace_id") for s in ss}) != 1]
    results.append(("each transaction on one connection and one request", not bad_txn and bool(by_txn),
                     f"{len(by_txn)} transactions, {len(bad_txn)} inconsistent"))

    # 3. pooling and interleaving really happened
    traces_per_conn: dict[int, set[str]] = defaultdict(set)
    for s in stmts:
        traces_per_conn[s["conn"]].add(s.get("trace_id"))
    shared = {c: len(t) for c, t in traces_per_conn.items() if len(t) > 1}
    ordered = sorted(stmts, key=lambda s: s["t_send_ns"])
    spans = {}
    for i, s in enumerate(ordered):
        tid = s.get("trace_id")
        first, _ = spans.get(tid, (i, i))
        spans[tid] = (first, i)
    interleaved = sum(
        1 for a, (fa, la) in spans.items() for b, (fb, lb) in spans.items() if a < b and fa < lb and fb < la)
    results.append(("pooled connections shared by different requests", bool(shared),
                     f"connections used: {len(traces_per_conn)}, requests per shared connection: {shared}"))
    results.append(("requests' statements interleaved in time", interleaved > 0,
                     f"{interleaved} interleaving request pairs"))

    # 4. CDC <-> proxy transaction join by xid
    committed = {str(e["xid"]): e for e in txn_ends if e["outcome"] == "COMMIT" and e.get("xid")}
    joined = [c for c in cdc if str(c["xid"]) in committed]
    unjoined_cdc = [c["xid"] for c in cdc if str(c["xid"]) not in committed]
    writers = {e["txn"] for e in txn_ends if e["outcome"] == "COMMIT"
               and any(s.get("tag", "").startswith(WRITE_TAGS) and not s.get("tag", "").endswith(" 0")
                       for s in by_txn.get(e["txn"], []))}
    cdc_xids = {str(c["xid"]) for c in cdc}
    writers_without_cdc = [t for t in writers
                           if not any(committed[x]["txn"] == t for x in cdc_xids if x in committed)]
    results.append(("every CDC transaction joins to one proxy transaction by xid",
                     not unjoined_cdc and bool(cdc),
                     f"{len(cdc)} CDC txns, {len(joined)} joined, unjoined xids: {unjoined_cdc}"))
    results.append(("every committed writing transaction has a CDC record", not writers_without_cdc,
                     f"{len(writers)} writing txns, {len(writers_without_cdc)} without CDC"))
    joined_traces_ok = all(committed[str(c["xid"])]["trace_id"] in requests for c in joined)
    results.append(("joined transactions belong to recorded HTTP requests", joined_traces_ok and bool(joined), ""))

    # 5. inserted rows agree between CDC and the proxied RETURNING rows
    mismatches = 0
    checked = 0
    for c in joined:
        txn = committed[str(c["xid"])]["txn"]
        returned_ids = set()
        for s in by_txn.get(txn, []):
            if s["sql_norm"].startswith('INSERT INTO "public"."Order"') and s.get("rows"):
                cols = s.get("columns") or []
                if "id" in cols:
                    returned_ids |= {r[cols.index("id")] for r in s["rows"]}
        for ch in c["changes"]:
            if ch["op"] == "insert" and ch["table"] == "Order":
                checked += 1
                if ch["new"]["id"] not in returned_ids:
                    mismatches += 1
    results.append(("CDC inserted Order rows match proxied INSERT ... RETURNING", checked > 0 and mismatches == 0,
                     f"{checked} rows checked, {mismatches} mismatches"))

    width = max(len(n) for n, _, _ in results)
    print(f"M1 acceptance (run {out.name}); HTTP statuses: {statuses}")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")

    # Informational: what the planted bugs did in this run (not part of M1 acceptance).
    stock_updates = [s for s in stmts if s["sql_norm"].startswith('UPDATE "public"."Product"')]
    print(f"\n  info: {len(stock_updates)} stock updates; "
          f"checkouts of the last item returned {statuses[0]} and {statuses[1]}; "
          f"same-key retries returned {statuses[2]} and {statuses[3]}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
