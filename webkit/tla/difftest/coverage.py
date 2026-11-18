#!/usr/bin/env python3
"""Summarize which PostgreSQL behaviors a set of dbdiff traces exercised.

    coverage.py TRACE_DIR [TRACE_DIR ...]

A differential pass only means something for behaviors that actually occur
in the traces, so this reports how often each one did.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    c: Counter[str] = Counter()
    traces = sorted(t for d in sys.argv[1:] for t in Path(d).glob("*.ndjson"))
    for path in traces:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        isos = {e["iso"] for e in events if e["ev"] == "start" and e["kind"] == "begin"}
        explicit = {e["txn"] for e in events if e["ev"] == "start" and e["kind"] == "begin"}
        c["traces"] += 1
        c["traces with mixed isolation levels"] += len(isos) > 1
        waited_in_trace = False
        for i, e in enumerate(events):
            if e["ev"] == "start":
                if e["kind"] not in ("begin", "commit", "rollback") and e["txn"] not in explicit:
                    c["autocommit statements"] += 1
                nxt = events[i + 1] if i + 1 < len(events) else None
                if not (nxt and nxt["ev"] == "done" and nxt["txn"] == e["txn"]):
                    c["statements that waited on a lock"] += 1
                    waited_in_trace = True
            elif e["ev"] == "done":
                c[f"done {e['kind']}"] += 1
                if not e["ok"]:
                    c[f"error {e['code']}"] += 1
                if e["kind"] == "commit" and e["tag"] == "ROLLBACK":
                    c["COMMIT of a failed transaction (ROLLBACK)"] += 1
                if e["kind"] in ("update", "delete", "lock") and e["ok"] and not e["rows"]:
                    c["write statements affecting 0 rows"] += 1
        c["traces with at least one lock wait"] += waited_in_trace

    width = max(len(k) for k in c)
    for k in sorted(c, key=lambda k: (not k.startswith("traces"), k)):
        print(f"  {k.ljust(width)}  {c[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
