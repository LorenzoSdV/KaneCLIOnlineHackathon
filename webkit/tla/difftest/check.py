#!/usr/bin/env python3
"""Check recorded PostgreSQL executions against WebDB.tla with TLC.

    check.py TRACE_DIR [TRACE_DIR ...] [--jobs N] [--report FILE]

For every *.ndjson trace (written by cmd/dbdiff) TLC searches for a WebDB
behavior that reproduces it exactly. A trace is ACCEPTED when TLC reports the
TraceNotDone invariant violated (some behavior consumed every event). A
REJECTED trace means WebDB cannot produce something real PostgreSQL did: a
model bug. For each rejection the checker locates the first event WebDB
cannot reproduce.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
TLA_LIB = HERE.parent
REPO = HERE.parents[2]
LIB = REPO / "Specula" / "lib"
CLASSPATH = f"{LIB / 'tla2tools.jar'}{os.pathsep}{LIB / 'CommunityModules-deps.jar'}"
TEMPLATE = (HERE / "TraceWebDB.cfg.tmpl").read_text()


def tlc(trace: Path, cut: str, workdir: Path) -> tuple[str, str]:
    """Run TLC once. Returns ("matched" | "unmatched" | "error", output)."""
    cfg = workdir / "trace.cfg"
    cfg.write_text(TEMPLATE.replace("@TRACE@", str(trace)).replace("@CUT@", cut))
    cmd = ["java", "-XX:+UseSerialGC", "-Xss16m", f"-DTLA-Library={TLA_LIB}", "-cp", CLASSPATH,
           "tlc2.TLC", "-config", str(cfg), "-workers", "1", "-noGenerateSpecTE",
           "-metadir", str(workdir / "meta"), str(HERE / "TraceWebDB.tla")]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir, timeout=600)
    out = p.stdout + p.stderr
    if "Invariant TraceNotDone is violated" in out:
        return "matched", out
    if p.returncode == 0 and "Model checking completed. No error has been found." in out:
        return "unmatched", out
    return "error", out


def first_mismatch(trace: Path, n_events: int, workdir: Path) -> int:
    """Largest prefix WebDB reproduces, by binary search; returns the 1-based failing line."""
    lo, hi = 1, n_events  # prefix up to line lo is matched (line 1 is the header)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        verdict, _ = tlc(trace, f"= {mid}", workdir)
        if verdict == "matched":
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def check(trace: Path) -> dict:
    lines = trace.read_text().splitlines()
    with tempfile.TemporaryDirectory(prefix="wkdiff-") as tmp:
        work = Path(tmp)
        verdict, out = tlc(trace, "<- FullLen", work)
        result = {"trace": str(trace), "verdict": verdict, "events": len(lines) - 1}
        if verdict == "unmatched":
            line = first_mismatch(trace, len(lines), work)
            result["first_unmatched_line"] = line
            result["event"] = json.loads(lines[line - 1]) if line <= len(lines) else None
        elif verdict == "error":
            result["tlc_output"] = out[-3000:]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    traces = sorted(t for d in args.dirs for t in d.glob("*.ndjson"))
    if not traces:
        sys.exit("no traces found")
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(check, traces))

    counts = {v: sum(r["verdict"] == v for r in results) for v in ("matched", "unmatched", "error")}
    print(f"{len(results)} traces: {counts['matched']} accepted, "
          f"{counts['unmatched']} REJECTED, {counts['error']} errors")
    for r in results:
        if r["verdict"] == "unmatched":
            print(f"  REJECTED {r['trace']}: first unmatched line {r['first_unmatched_line']}: "
                  f"{json.dumps(r['event'])}")
        elif r["verdict"] == "error":
            print(f"  ERROR {r['trace']}:\n{r['tlc_output']}")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2))
    return 0 if counts["matched"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
