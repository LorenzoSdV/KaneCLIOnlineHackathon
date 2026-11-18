#!/usr/bin/env python3
"""Check that WebDB reaches the anomalies PostgreSQL allows, and only those.

Runs Anomalies.tla for every (scenario, isolation level) and compares TLC's
verdict with PostgreSQL's documented behavior.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LIB = REPO / "Specula" / "lib"
CP = f"{LIB / 'tla2tools.jar'}{os.pathsep}{LIB / 'CommunityModules-deps.jar'}"

# (scenario, iso) -> True if PostgreSQL allows the anomaly, i.e. Property must be violated.
# SERIALIZABLE write skew: PostgreSQL prevents it, but WebDB v1 over-approximates SSI
# (snapshot isolation + allowed aborts), so it is expected to be reachable in the model.
EXPECTED = {
    ("lost_update", "RC"): True, ("lost_update", "RR"): False, ("lost_update", "SER"): False,
    ("atomic_increment", "RC"): False, ("atomic_increment", "RR"): False, ("atomic_increment", "SER"): False,
    ("oversell", "RC"): True, ("oversell", "RR"): False, ("oversell", "SER"): False,
    ("write_skew", "RC"): True, ("write_skew", "RR"): True, ("write_skew", "SER"): True,
}
OVER_APPROX = {("write_skew", "SER")}

CFG = """SPECIFICATION Spec
CONSTANTS
    Txns = {{"A", "B"}}
    Tables = {{"t"}}
    Keys = {{1, 2}}
    UniqueCols <- ScenarioUniqueCols
    InitRows <- ScenarioInitRows
    AutoIso = "RC"
    Absent = Absent
    NoTxn = NoTxn
    NoWrite = NoWrite
    Scenario = "{scenario}"
    Iso = "{iso}"
INVARIANT {inv}
CHECK_DEADLOCK FALSE
"""


def run(scenario: str, iso: str, inv: str = "Property") -> str:
    with tempfile.TemporaryDirectory(prefix="wkanom-") as tmp:
        cfg = Path(tmp) / "a.cfg"
        cfg.write_text(CFG.format(scenario=scenario, iso=iso, inv=inv))
        p = subprocess.run(
            ["java", "-XX:+UseSerialGC", f"-DTLA-Library={HERE.parent}", "-cp", CP, "tlc2.TLC",
             "-config", str(cfg), "-workers", "1", "-noGenerateSpecTE", "-metadir", str(Path(tmp) / "m"),
             str(HERE / "Anomalies.tla")],
            capture_output=True, text=True, cwd=tmp, timeout=600)
        out = p.stdout + p.stderr
        if f"Invariant {inv} is violated" in out:
            return "violated"
        if "No error has been found" in out:
            return "holds"
        return "error:\n" + out[-2000:]


def main() -> int:
    failures = 0
    print(f"{'scenario':18} {'iso':4} {'expected':10} {'WebDB':10}")
    for (scenario, iso), allowed in EXPECTED.items():
        got = run(scenario, iso)
        want = "violated" if allowed else "holds"
        # Non-vacuity: a serial execution, where both actors commit, must be reachable.
        live = run(scenario, iso, "NotBothCommitted") == "violated"
        mark = "ok" if got == want and live else ("MISMATCH" if live else "VACUOUS")
        note = "  (model over-approximates SSI; PostgreSQL prevents this)" if (scenario, iso) in OVER_APPROX else ""
        failures += got != want or not live
        print(f"{scenario:18} {iso:4} {want:10} {got.splitlines()[0]:10} {mark}{note}")
        if got.startswith("error"):
            print(got)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
