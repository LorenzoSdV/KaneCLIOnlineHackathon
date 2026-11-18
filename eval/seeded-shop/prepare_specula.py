#!/usr/bin/env python3
"""Prepare a Specula Phase-3 workspace for seeded-shop (M4 plumbing test).

    prepare_specula.py --traces RUN/traces --out runs/specula-seeded-shop

Lays out what Phases 1-2.5 would have produced for a web app, so `specula validate` can run:

  .specula-output/
    modeling-brief.md          scope and Scenarios, from the README's stated guarantees only
    spec/                      base.tla (wraps Shop), MC.tla + cfgs, Trace.tla (wraps TraceShop),
                               WebDB/WebTrace modules, instrumentation-spec.md (from mapping.toml)
    traces/*.ndjson            recorded traces (webkit/tracebuilder)
    harness/run.sh             regenerates traces through the tracing stack
    .prompt-extra.md           web-profile instructions (until M6 turns them into skills)

Nothing here reveals the answer key: the brief uses only what the app's README promises.
"""

from __future__ import annotations

import argparse
import shutil
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SPEC = HERE / "spec"
TLA = REPO / "webkit" / "tla"

BRIEF = """# Modeling Brief: seeded-shop

## 1. System overview

A small shop HTTP API (Node.js, Fastify, Prisma 7 on PostgreSQL 17). Requests run concurrently on a
connection pool; checkout and cancel run in interactive transactions at PostgreSQL's default isolation
(READ COMMITTED). System category: **W (web / request-concurrent)**: many concurrent request handlers
over a shared transactional store. Database semantics come from the reusable `WebDB.tla` module.

## 2. Contract (README "Checkout guarantees")

- An order is only placed when enough stock is available; stock never goes negative.
- Each coupon can be redeemed at most once per user.
- Clients may retry a checkout safely by sending the same `Idempotency-Key`: a retried request returns
  the original order instead of placing a new one.

## 3. Scenarios

### Scenario 1: concurrent checkouts of limited stock
Mechanism: `POST /checkout` reads the product, checks `stock >= quantity`, inserts the order, then
decrements stock, in separate statements of one transaction (src/server.ts:L98-L117).
Invariant: `NoNegativeStock`. Hunt config: `MC_hunt_last_item.cfg`.

### Scenario 2: client retry overlapping the original request
Mechanism: the Idempotency-Key lookup (src/server.ts:L92) runs before the transaction.
Invariant: `OneOrderPerIdempotencyKey`. Hunt config: `MC_hunt_retry.cfg`.

### Scenario 3: concurrent redemptions of one coupon
Mechanism: the redemption insert runs inside the checkout transaction (src/server.ts:L115).
Invariant: `CouponOncePerUser`. Hunt config: `MC_hunt_coupon.cfg`.

## 4. Model

`spec/Shop.tla` (wrapped by `base.tla`): one action per SQL statement each handler issues; lock-waiting
statements are split into _start/_finish. `MC.tla` runs a few concurrent requests from the seeded state;
`MC.cfg` is the convergence configuration (all inputs).
"""

PROMPT_EXTRA = """This target is a **web application** (Category W). Follow the installed skills, with these
web-profile conventions, which take precedence where they differ:

1. **Trace validation is existential and uses the webkit checker, not `TraceMatched`.** Traces of
   concurrent requests leave interleavings open (events of different requests may match in either order),
   so a trace is valid when *some* behavior reproduces it. Do NOT add `PROPERTIES TraceMatched` to the
   trace config and do not use `run_trace_validation`. Validate all traces with:

       python3 {repo}/webkit/tracecheck/check.py --spec {wd}/spec/Trace.tla \\
           --template {wd}/spec/Trace.cfg.tmpl {wd}/traces

   It reports ACCEPTED, or for a rejected trace the most events matched and the recorded events the model
   could not match next (where model and implementation diverge).
2. **Model checking** uses `spec/MC.tla` with `MC.cfg` (convergence) and `MC_hunt_*.cfg` (hunting).
   Run TLC from `{wd}/spec`; every module the specs need is in that directory.
3. **Concurrency is normal operation, not a fault.** Concurrent requests stay enabled in the convergence
   run, so a real race can show up as Case C there.
4. **`WebDB.tla` and `WebTrace.tla` are read-only shared library modules.** Never edit them. If evidence
   shows their semantics are wrong for this app, record it in changelog.md and stop that line of repair.
5. Harness: `harness/run.sh` regenerates traces through the tracing stack (needs Docker). The traces in
   `traces/` are current; regenerate only if you change the mapping or the workloads.
"""


def instrumentation_spec(mapping: dict) -> str:
    lines = ["# Instrumentation spec: seeded-shop", "",
             "Boundary tracing (webkit): no code changes. Each model action is identified by the HTTP route or",
             "the SQL statement shape below (eval/seeded-shop/mapping.toml). Statements that can wait on locks",
             "become two trace events, `<action>_start` and `<action>_finish`.", "",
             "## Routes", "", "| Action | Request |", "|---|---|"]
    lines += [f"| `{r['action']}_Begin` / `{r['action']}_End` | `{r['match']}` |" for r in mapping["route"]]
    lines += ["", "## SQL statements", "", "| Action | Kind | Table | Statement shape |", "|---|---|---|---|"]
    for s in mapping["sql"]:
        lines.append(f"| `{s['action']}` | {s['kind']} | {s.get('table', '')} | `{s['match']}` |")
    lines += ["", "## Modelled columns", ""]
    for t, spec in mapping["tables"].items():
        lines.append(f"- `{t}` (key `{spec['key']}`): " + ", ".join(f"`{c}`" for c in spec["columns"]))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--traces", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    wd = args.out.resolve() / ".specula-output"
    if wd.exists():
        shutil.rmtree(wd)
    spec = wd / "spec"
    spec.mkdir(parents=True)
    for f in ["Shop.tla", "TraceShop.tla", "MC.tla", "MC.cfg", "MC_hunt_last_item.cfg",
              "MC_hunt_retry.cfg", "MC_hunt_coupon.cfg"]:
        shutil.copy(SPEC / f, spec / f)
    for f in ["WebDB.tla", "WebTrace.tla"]:
        shutil.copy(TLA / f, spec / f)
    (spec / "base.tla").write_text("---- MODULE base ----\n\\* The app model; see Shop.tla.\nEXTENDS Shop\n====\n")
    (spec / "Trace.tla").write_text("---- MODULE Trace ----\n\\* Trace validation; see TraceShop.tla.\n"
                                    "EXTENDS TraceShop\n====\n")
    shutil.copy(SPEC / "TraceShop.cfg.tmpl", spec / "Trace.cfg.tmpl")
    with (HERE / "mapping.toml").open("rb") as f:
        mapping = tomllib.load(f)
    (spec / "instrumentation-spec.md").write_text(instrumentation_spec(mapping))
    (spec / "changelog.md").write_text("# Changelog\n")

    (wd / "modeling-brief.md").write_text(BRIEF)
    (wd / ".prompt-extra.md").write_text(PROMPT_EXTRA.format(repo=REPO, wd=wd))
    traces = wd / "traces"
    traces.mkdir()
    for t in sorted(args.traces.glob("*.ndjson")):
        shutil.copy(t, traces / t.name)

    harness = wd / "harness"
    harness.mkdir()
    (harness / "run.sh").write_text(f"""#!/usr/bin/env bash
# Regenerate seeded-shop traces through the webkit tracing stack.
set -euo pipefail
cd {REPO}
python3 webkit/stack/wkstack.py up --config eval/seeded-shop/stack.toml
python3 eval/seeded-shop/workloads.py --repeat 3
RUN=$(python3 -c 'import json;print(json.load(open("runs/.current-seeded-shop.json"))["out_dir"])')
cp "$RUN"/traces/*.ndjson {traces}/
""")
    (harness / "run.sh").chmod(0o755)
    (harness / "INSTRUMENTATION.md").write_text(
        "No code instrumentation: the webkit stack records HTTP, SQL and committed changes at the app's\n"
        "boundaries (see webkit/README.md). To change what is captured, edit eval/seeded-shop/mapping.toml.\n")
    print(f"workspace ready: {wd} ({len(list(traces.glob('*.ndjson')))} traces)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
