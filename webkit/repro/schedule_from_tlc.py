#!/usr/bin/env python3
"""Model-check one config and turn its counterexample into a reproduction schedule (generic).

    schedule_from_tlc.py --spec spec/MC.tla --config spec/MC_hunt_x.cfg --invariant Inv --out schedule.json

Runs TLC with -dumpTrace json, then webkit/timeline/timeline.py --schedule-out. Prints the
timeline. Exits non-zero if the config has no violation (nothing to reproduce).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "Specula" / "lib"
CP = f"{LIB / 'tla2tools.jar'}{os.pathsep}{LIB / 'CommunityModules-deps.jar'}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--invariant", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="wksched-") as tmp:
        ce = Path(tmp) / "ce.json"
        p = subprocess.run(
            ["java", "-XX:+UseParallelGC", f"-DTLA-Library={REPO / 'webkit' / 'tla'}", "-cp", CP, "tlc2.TLC",
             "-config", str(args.config.resolve()), "-workers", "auto", "-noGenerateSpecTE",
             "-dumpTrace", "json", str(ce), "-metadir", str(Path(tmp) / "meta"), str(args.spec.resolve())],
            capture_output=True, text=True, cwd=args.spec.resolve().parent)
        if f"Invariant {args.invariant} is violated" not in p.stdout + p.stderr or not ce.exists():
            print((p.stdout + p.stderr)[-2000:])
            sys.exit(f"no violation of {args.invariant} in {args.config.name}: nothing to reproduce")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.run([sys.executable, str(REPO / "webkit" / "timeline" / "timeline.py"), str(ce),
                               "--invariant", args.invariant, "--schedule-out", str(args.out)]).returncode


if __name__ == "__main__":
    sys.exit(main())
