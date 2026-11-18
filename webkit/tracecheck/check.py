#!/usr/bin/env python3
"""Validate an app model against recorded traces with TLC (generic).

    check.py --spec spec/TraceApp.tla --template spec/TraceApp.cfg.tmpl TRACE [TRACE|DIR ...]

The trace spec must EXTEND webkit/tla/WebTrace.tla. The .cfg template contains @TRACE@ (trace
path) and @CUT@ (number of events to match). A trace is ACCEPTED when TLC finds a behavior of
the model that matches every event (it reports TraceNotDone violated).

For a REJECTED trace the checker finds the largest number of events the model can match,
reads the matched set from TLC's counterexample, and reports the events that were ready to
match next but could not be: that is where model and implementation disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "Specula" / "lib"
TLA_LIB = REPO / "webkit" / "tla"
CLASSPATH = f"{LIB / 'tla2tools.jar'}{os.pathsep}{LIB / 'CommunityModules-deps.jar'}"
DONE_RE = re.compile(r"/\\ done = \{([^}]*)\}")


def tlc(spec: Path, template: str, trace: Path, cut: str, work: Path) -> tuple[str, str]:
    cfg = work / "trace.cfg"
    cfg.write_text(template.replace("@TRACE@", str(trace.resolve())).replace("@CUT@", cut))
    cmd = ["java", "-XX:+UseParallelGC", "-Xss16m", f"-DTLA-Library={TLA_LIB}", "-cp", CLASSPATH,
           "tlc2.TLC", "-config", str(cfg), "-workers", "auto", "-noGenerateSpecTE",
           "-metadir", str(work / "meta"), str(spec.resolve())]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=spec.resolve().parent, timeout=3600)
    out = p.stdout + p.stderr
    if "Invariant TraceNotDone is violated" in out:
        return "matched", out
    if "Model checking completed. No error has been found." in out:
        return "unmatched", out
    return "error", out


def events(trace: Path) -> list[dict]:
    return [json.loads(line) for line in trace.read_text().splitlines()][1:]


def ready_after(evs: list[dict], done: set[int]) -> list[int]:
    """Events (1-based) whose predecessors are all matched, mirroring WebTrace.Ready."""
    out = []
    for j, ej in enumerate(evs, 1):
        if j in done:
            continue
        preds = [i for i, ei in enumerate(evs, 1)
                 if i != j and ((ei["actor"] == ej["actor"] and i < j) or ei["hi"] < ej["lo"])]
        if all(i in done for i in preds):
            out.append(j)
    return out


def check(spec: Path, template: str, trace: Path) -> dict:
    evs = events(trace)
    with tempfile.TemporaryDirectory(prefix="wktrace-") as tmp:
        work = Path(tmp)
        verdict, out = tlc(spec, template, trace, "<- TraceLen", work)
        result: dict = {"trace": str(trace), "verdict": verdict, "events": len(evs)}
        if verdict == "error":
            result["tlc_output"] = out[-4000:]
        elif verdict == "unmatched":
            lo, hi = 0, len(evs) - 1          # lo events are known to match
            while lo < hi:
                mid = (lo + hi + 1) // 2
                v, _ = tlc(spec, template, trace, f"= {mid}", work)
                if v == "matched":
                    lo = mid
                else:
                    hi = mid - 1
            result["max_matched"] = lo
            done: set[int] = set()
            if lo > 0:
                _, out = tlc(spec, template, trace, f"= {lo}", work)
                found = DONE_RE.findall(out)
                if found:
                    done = {int(x) for x in found[-1].replace(" ", "").split(",") if x}
            result["stuck_at"] = [{"event": j, "actor": evs[j - 1]["actor"], "action": evs[j - 1]["action"]}
                                  for j in ready_after(evs, done)]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--report", type=Path)
    ap.add_argument("traces", nargs="+", type=Path)
    args = ap.parse_args()
    template = args.template.read_text()
    traces = sorted(t for p in args.traces for t in ([p] if p.is_file() else p.glob("*.ndjson")))
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda t: check(args.spec, template, t), traces))

    ok = sum(r["verdict"] == "matched" for r in results)
    print(f"{len(results)} traces: {ok} accepted, {len(results) - ok} not accepted")
    for r in results:
        name = Path(r["trace"]).name
        if r["verdict"] == "matched":
            print(f"  ACCEPTED  {name} ({r['events']} events)")
        elif r["verdict"] == "unmatched":
            stuck = ", ".join(f"#{s['event']} {s['actor']} {s['action']}" for s in r["stuck_at"])
            print(f"  REJECTED  {name}: model matches at most {r['max_matched']}/{r['events']} events; "
                  f"could not match next: {stuck}")
        else:
            print(f"  ERROR     {name}:\n{r['tlc_output']}")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
