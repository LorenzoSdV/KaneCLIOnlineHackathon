#!/usr/bin/env python3
"""Grade the M5b loop tests from the artifacts Specula left in each workspace.

    check_loops.py [L1 L2 L3 | L2=runs/loop-L2-try1 ...]

L1  conformance back-edge: the model (missing its stock guard) was repaired in Phase 3, so every trace
    now passes, and hunting still reports the real races.
L2  INVARIANT back-edge: the over-strong invariant's finding went PENDING REPAIR -> repair request
    CONSUMED -> final verdict FALSE POSITIVE, with no DEFERRED request.
L3  SPEC_REPAIR back-edge from lock-wait evidence: the restock finding's repair request is a SPEC_REPAIR
    citing the lock (LockBlocked / FOR UPDATE), CONSUMED; the repaired model locks the row and passes
    every rebuilt trace; final verdict FALSE POSITIVE.
All: WebDB.tla and WebTrace.tla untouched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHECK = REPO / "webkit" / "tracecheck" / "check.py"


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.split("#")[0].strip()
    return out


def statuses(wd: Path) -> list[str]:
    cb = wd / "confirmed-bugs.md"
    return re.findall(r"\*\*Status\*\*:\s*`?([A-Z][A-Z _]+[A-Z])", cb.read_text()) if cb.exists() else []


def traces_pass(wd: Path) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, str(CHECK), "--spec", str(wd / "spec" / "Trace.tla"),
                        "--template", str(wd / "spec" / "Trace.cfg.tmpl"), "--jobs", "2", str(wd / "traces")],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout.splitlines() or ["?"])[0]


def library_untouched(wd: Path) -> bool:
    return all((wd / "spec" / f).read_bytes() == (REPO / "webkit" / "tla" / f).read_bytes()
               for f in ("WebDB.tla", "WebTrace.tla"))


def grade(case: str, workspace: Path | None = None) -> list[tuple[str, bool, str]]:
    wd = (workspace or REPO / "runs" / f"loop-{case}") / ".specula-output"
    rrs = sorted((wd / "spec" / "repair-requests").glob("RR-*.md")) if (wd / "spec" / "repair-requests").exists() else []
    deferred = list((wd / "spec" / "repair-requests" / "deferred").glob("RR-*.md")) \
        if (wd / "spec" / "repair-requests" / "deferred").exists() else []
    checks = [("WebDB.tla and WebTrace.tla untouched", library_untouched(wd), "")]
    if case == "L1":
        ok, line = traces_pass(wd)
        changelog = (wd / "spec" / "changelog.md").read_text() if (wd / "spec" / "changelog.md").exists() else ""
        checks += [("model repaired: every trace passes", ok, line),
                    ("changelog records a spec change", len(changelog.strip().splitlines()) > 2, ""),
                    ("bug report written", (wd / "spec" / "bug-report.md").exists(), "")]
    elif case in ("L2", "L3"):
        fm = [frontmatter(r) for r in rrs]
        want_target = "INVARIANT" if case == "L2" else "SPEC_REPAIR"
        consumed = [f for f in fm if f.get("status") == "CONSUMED" and f.get("target") == want_target]
        st = statuses(wd)
        checks += [(f"a {want_target} repair request was CONSUMED", bool(consumed),
                    "; ".join(f"{r.name}: {f.get('target')}/{f.get('status')}" for r, f in zip(rrs, fm)) or "none"),
                   ("no request DEFERRED", not deferred, f"{len(deferred)} deferred"),
                   ("final verdict FALSE POSITIVE", "FALSE POSITIVE" in st, ", ".join(st) or "no verdicts")]
        if case == "L3":
            evidence = " ".join(r.read_text() for r in rrs)
            shop = (wd / "spec" / "Shop.tla").read_text()
            ok, line = traces_pass(wd)
            checks += [("repair request cites the lock", bool(re.search(r"LockBlocked|FOR UPDATE", evidence)), ""),
                       ("repaired model locks the row (WriteStart ... \"lock\")",
                        bool(re.search(r'WriteStart\(r,\s*"lock",\s*"Product"', shop)), ""),
                       ("repaired model passes every (rebuilt) trace", ok, line)]
    return checks


def main() -> int:
    cases = sys.argv[1:] or ["L1", "L2", "L3"]
    failed = 0
    for arg in cases:
        case, _, path = arg.partition("=")
        print(f"{arg}:")
        for name, ok, detail in grade(case, (REPO / path) if path else None):
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
