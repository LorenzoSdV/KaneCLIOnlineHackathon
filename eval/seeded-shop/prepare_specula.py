#!/usr/bin/env python3
"""Prepare a Specula workspace for seeded-shop, optionally with one planted model defect.

    prepare_specula.py --run RUN_DIR --out runs/specula-seeded-shop [--variant base|L1|L2|L3]

Lays out what Phases 1-2.5 would have produced for a web app, so `specula validate` / `specula run
--skip-analysis --skip-specgen --skip-harness` can start from it:

  .specula-output/
    modeling-brief.md          scope and Scenarios (from the README's stated guarantees)
    spec/                      base.tla (wraps Shop), MC.tla + cfgs, Trace.tla (wraps TraceShop),
                               WebDB/WebTrace modules, instrumentation-spec.md
    traces/*.ndjson            traces built from RUN_DIR's recordings with harness/mapping.toml
    harness/mapping.toml       this workspace's mapping (a repair may change it)
    harness/rebuild_traces.sh  rebuild traces/ from the recordings after a mapping change
    harness/run.sh             record fresh traces through the tracing stack
    .prompt-extra.md           web-profile instructions (until M6 turns them into skills)

Variants plant one model defect each, to exercise one loop back-edge (M5b loop tests):

  base  the reference model
  L1    the model drops checkout's stock guard         -> trace validation fails; Phase 3 repairs it
  L2    an over-strong invariant the app never promises -> PENDING REPAIR (INVARIANT) -> FALSE POSITIVE
  L3    restock's SELECT ... FOR UPDATE modelled and mapped as a plain SELECT; the workspace lacks the
        concurrent-restock traces (a coverage gap)  -> LockBlocked -> SPEC_REPAIR -> FALSE POSITIVE

Nothing here reveals the answer key: briefs use only what the README promises (L2 adds one
mistaken scenario on purpose).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SPEC = HERE / "spec"
TLA = REPO / "webkit" / "tla"
sys.path.insert(0, str(REPO / "webkit" / "tracebuilder"))
import build  # noqa: E402

HUNTS = {"base": ["MC_hunt_last_item.cfg", "MC_hunt_retry.cfg", "MC_hunt_coupon.cfg", "MC_hunt_restock.cfg"],
         "L1": ["MC_hunt_last_item.cfg", "MC_hunt_retry.cfg", "MC_hunt_coupon.cfg", "MC_hunt_restock.cfg"],
         "L2": ["MC_hunt_orders.cfg"],
         "L3": ["MC_hunt_restock.cfg"]}

BRIEF_HEAD = """# Modeling Brief: seeded-shop

## 1. System overview

A small shop HTTP API (Node.js, Fastify, Prisma 7 on PostgreSQL 17). Requests run concurrently on a
connection pool; checkout, cancel and restock run in interactive transactions at PostgreSQL's default
isolation (READ COMMITTED). System category: **W (web / request-concurrent)**: many concurrent request
handlers over a shared transactional store. Database semantics come from the reusable `WebDB.tla` module.

## 2. Contract (README)

- An order is only placed when enough stock is available; stock never goes negative.
- Each coupon can be redeemed at most once per user.
- Clients may retry a checkout safely by sending the same `Idempotency-Key`.
- Concurrent restocks all count.

## 3. Scenarios
"""

SCENARIOS = {
    "MC_hunt_last_item.cfg": """
### Concurrent checkouts of limited stock
Mechanism: `POST /checkout` reads the product, checks the stock, inserts the order, then decrements
stock, in separate statements of one transaction (src/server.ts:L98-L117).
Invariant: `NoNegativeStock`. Hunt config: `MC_hunt_last_item.cfg`.
""",
    "MC_hunt_retry.cfg": """
### Client retry overlapping the original request
Mechanism: the Idempotency-Key lookup (src/server.ts:L92) runs before the transaction.
Invariant: `OneOrderPerIdempotencyKey`. Hunt config: `MC_hunt_retry.cfg`.
""",
    "MC_hunt_coupon.cfg": """
### Concurrent redemptions of one coupon
Mechanism: the redemption insert runs inside the checkout transaction (src/server.ts:L115).
Invariant: `CouponOncePerUser`. Hunt config: `MC_hunt_coupon.cfg`.
""",
    "MC_hunt_restock.cfg": """
### Concurrent restocks of one product
Mechanism: `POST /products/:id/restock` reads the stock, computes the new value in the app and writes
it, in one transaction (src/server.ts:L137-L153).
Invariant: `RestocksAllCount`. Hunt config: `MC_hunt_restock.cfg`.
""",
    "MC_hunt_orders.cfg": """
### Repeat orders by one customer
Mechanism: a customer may place several checkouts; each inserts a PENDING order (src/server.ts:L111).
The shop is expected to keep at most one pending order per customer.
Invariant: `OneOpenOrderPerUser`. Hunt config: `MC_hunt_orders.cfg`.
""",
}

BRIEF_TAIL = """
## 4. Model

`spec/Shop.tla` (wrapped by `base.tla`): one action per SQL statement each handler issues; lock-waiting
statements are split into _start/_finish. Invariants are in `ShopInvariants.tla` and `MC.tla`. `MC.tla`
runs a few concurrent requests from the seeded state; `MC.cfg` is the convergence configuration.
"""

PROMPT_EXTRA = """This target is a **web application** (Category W). Follow the installed skills, with these
web-profile conventions, which take precedence where they differ.

Paths: toolkit `{webkit}`; spec `{wd}/spec`; traces `{wd}/traces`; mapping `{wd}/harness/mapping.toml`;
stack config `{repo}/eval/seeded-shop/stack.toml`; app source `{repo}/apps/seeded-shop`.

## Validation (Phase 3)

1. **Trace validation is existential and uses the webkit checker, not `TraceMatched`.** Traces of
   concurrent requests leave interleavings open, so a trace is valid when *some* behavior reproduces it.
   Do NOT add `PROPERTIES TraceMatched` and do not use `run_trace_validation`. Validate all traces with:

       python3 {webkit}/tracecheck/check.py --spec {wd}/spec/Trace.tla \\
           --template {wd}/spec/Trace.cfg.tmpl {wd}/traces

   A rejected trace reports the most events matched and the recorded events the model could not match
   next: where model and implementation diverge.
2. **Model checking** uses `spec/MC.tla` with `MC.cfg` (convergence) and the `MC_hunt_*.cfg` files. Run
   TLC from `{wd}/spec`; every module the specs need is in that directory.
3. **Concurrency is normal operation, not a fault.** Concurrent requests stay enabled in the convergence
   run.
4. **`WebDB.tla` and `WebTrace.tla` are read-only shared library modules.** Never edit them.
5. **If a repair changes how a statement is modelled** (for example a plain SELECT that is really a
   locking SELECT ... FOR UPDATE, or a step split into _start/_finish), update the statement's rule in
   `{wd}/harness/mapping.toml` to match, then rebuild the traces with `{wd}/harness/rebuild_traces.sh`
   before re-validating. The rebuild uses all recorded runs, including ones not yet in `traces/`.

## Confirmation (Phase 4)

Read the bug-confirmation skill's `references/web-reproduction.md`. Concretely for this target:

- Start the stack if `curl -s localhost:8080/health` fails:
  `python3 {webkit}/stack/wkstack.py up --config {repo}/eval/seeded-shop/stack.toml`
- Reset the database before each run with the app's seed command:
  `docker exec $(docker ps -q --filter label=com.docker.compose.project=seeded-shop --filter label=com.docker.compose.service=app) npm run -s db:seed`
- Seeded accounts: bearer tokens `tok-alice`, `tok-bob`, `tok-carol`.
- Schedule: `python3 {webkit}/repro/schedule_from_tlc.py --spec {wd}/spec/MC.tla --config {wd}/spec/<hunt cfg> --invariant <Invariant> --out <dir>/schedule.json`
- Replay: `scheduler.replay(schedule, send)` from `{webkit}/repro/scheduler.py`. A worked example of a
  complete repro test for this app: `{repo}/eval/seeded-shop/repro/test_bug1_oversell.py`.
- Same-bug check: `python3 {webkit}/tracecheck/check.py --spec {wd}/spec/Trace.tla --template {wd}/spec/Trace.cfg.tmpl --violates <Invariant> --invariants-module ShopInvariants <trace>`,
  with the trace built by `{webkit}/tracebuilder/build.py <recordings dir> --mapping {wd}/harness/mapping.toml --since-ns <t0> --until-ns <t1> --out <trace>`.
  The recordings dir is `out_dir` in `{repo}/runs/.current-seeded-shop.json`.
"""

L1_OLD = """       ELSE LET p == CHOOSE p \\in Rows(r) : TRUE IN
            IF p[2].stock < rq[r].quantity
            THEN ToRollback(r, "co_rollback", 409) /\\ UNCHANGED lv
            ELSE /\\ SetLv(r, [lv[r] EXCEPT !.price = p[2].priceCents,
                                           !.total = p[2].priceCents * rq[r].quantity])
                 /\\ Go(r, IF rq[r].couponCode # "" THEN "co_coupon" ELSE "co_ins")
                 /\\ UNCHANGED rs"""
L1_NEW = """       ELSE LET p == CHOOSE p \\in Rows(r) : TRUE IN
            /\\ SetLv(r, [lv[r] EXCEPT !.price = p[2].priceCents,
                                      !.total = p[2].priceCents * rq[r].quantity])
            /\\ Go(r, IF rq[r].couponCode # "" THEN "co_coupon" ELSE "co_ins")
            /\\ UNCHANGED rs"""

L2_INVARIANT = """
\\* Each customer holds at most one pending order.
OneOpenOrderPerUser ==
    \\A k1, k2 \\in CommittedKeys("Order") :
        LET o1 == Committed("Order", k1)
            o2 == Committed("Order", k2)
        IN (k1 # k2 /\\ o1.status = "PENDING" /\\ o2.status = "PENDING") => o1.userId # o2.userId
"""

L3_LOCK_START = '\\* L144-L145: SELECT "stock" ... FOR UPDATE (a locking read)'
L3_LOCK_END = '\\* L148: write the computed value (absolute)'
L3_READ = """\\* L144-L145: read the product's stock
Restock_ReadProduct(r) ==
    /\\ pc[r] = "rs_lock"
    /\\ Select(r, "Product", LAMBDA k, row : k = rq[r].productId)
    /\\ IF Rows(r) = {}
       THEN ToRollback(r, "rs_rollback", 404) /\\ UNCHANGED lv
       ELSE /\\ SetLv(r, [lv[r] EXCEPT !.total = (CHOOSE p \\in Rows(r) : TRUE)[2].stock + rq[r].quantity])
            /\\ Go(r, "rs_set") /\\ UNCHANGED rs
    /\\ UNCHANGED <<rq, nextId>>

"""


def replace_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        sys.exit(f"variant anchor not found exactly once: {what}")
    return text.replace(old, new)


def instrumentation_spec(mapping: dict) -> str:
    lines = ["# Instrumentation spec: seeded-shop", "",
             "Boundary tracing (webkit): no code changes. Each model action is identified by the HTTP route or",
             "the SQL statement shape in harness/mapping.toml. Statements that can wait on locks (kind write or",
             "insert) become two trace events, `<action>_start` and `<action>_finish`.", "",
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
    ap.add_argument("--run", type=Path, required=True, help="recordings dir with traces/windows.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--variant", choices=sorted(HUNTS), default="base")
    args = ap.parse_args()
    run = args.run.resolve()
    windows = json.loads((run / "traces" / "windows.json").read_text())

    wd = args.out.resolve() / ".specula-output"
    if wd.exists():
        shutil.rmtree(wd)
    spec, harness, traces = wd / "spec", wd / "harness", wd / "traces"
    for d in (spec, harness, traces):
        d.mkdir(parents=True)

    for f in ["Shop.tla", "ShopInvariants.tla", "TraceShop.tla", "MC.tla", "MC.cfg", *HUNTS["base"]]:
        if f in ("MC.cfg", "Shop.tla", "ShopInvariants.tla", "TraceShop.tla", "MC.tla") or f in HUNTS[args.variant]:
            shutil.copy(SPEC / f, spec / f)
    for f in ["WebDB.tla", "WebTrace.tla"]:
        shutil.copy(TLA / f, spec / f)
    (spec / "base.tla").write_text("---- MODULE base ----\n\\* The app model; see Shop.tla.\nEXTENDS Shop\n====\n")
    (spec / "Trace.tla").write_text("---- MODULE Trace ----\n\\* Trace validation; see TraceShop.tla.\n"
                                    "EXTENDS TraceShop\n====\n")
    shutil.copy(SPEC / "TraceShop.cfg.tmpl", spec / "Trace.cfg.tmpl")
    (spec / "changelog.md").write_text("# Changelog\n")
    shutil.copy(HERE / "mapping.toml", harness / "mapping.toml")

    # Plant the variant's defect.
    shop, mapping_text = (spec / "Shop.tla").read_text(), (harness / "mapping.toml").read_text()
    excluded: set[str] = set()
    if args.variant == "L1":
        shop = replace_once(shop, L1_OLD, L1_NEW, "L1 stock guard")
    elif args.variant == "L2":
        inv = (spec / "ShopInvariants.tla").read_text()
        inv = replace_once(inv, "=============================================================================",
                           L2_INVARIANT + "\n=============================================================================",
                           "L2 invariants end")
        (spec / "ShopInvariants.tla").write_text(inv)
        cfg = (SPEC / "MC_hunt_last_item.cfg").read_text()
        cfg = cfg.replace("Inputs <- LastItemInputs", "Inputs <- RetryFreeOrderInputs").replace(
            "    NoNegativeStock", "    OneOpenOrderPerUser")
        (spec / "MC_hunt_orders.cfg").write_text(cfg)
        mc = (spec / "MC.tla").read_text()
        mc = replace_once(mc, "AllInputs      ==",
                          'RetryFreeOrderInputs == {In("Checkout", "tok-bob", 2, 1, "", "", 0)}\nAllInputs      ==',
                          "L2 inputs")
        (spec / "MC.tla").write_text(mc)
    elif args.variant == "L3":
        start, end = shop.index(L3_LOCK_START), shop.index(L3_LOCK_END)
        shop = shop[:start] + L3_READ + shop[end:]
        shop = replace_once(shop, '''      [] pc[r] = "rs_lock"     -> "Restock_LockProduct_start"
      [] pc[r] = "rs_lock_f"   -> "Restock_LockProduct_finish"
''', '''      [] pc[r] = "rs_lock"     -> "Restock_ReadProduct"
''', "L3 step names")
        shop = replace_once(shop, "\\/ Restock_LockProduct_start(r) \\/ Restock_LockProduct_finish(r)",
                            "\\/ Restock_ReadProduct(r)", "L3 step list")
        mapping_text = replace_once(mapping_text, '''action = "Restock_LockProduct"
kind = "write"''', '''action = "Restock_ReadProduct"
kind = "select"''', "L3 mapping rule")
        excluded = {name for name in windows if name.startswith("restock_race")}
    (spec / "Shop.tla").write_text(shop)
    (harness / "mapping.toml").write_text(mapping_text)
    mapping = tomllib.loads(mapping_text)
    (spec / "instrumentation-spec.md").write_text(instrumentation_spec(mapping))

    # Traces from the recordings, with this workspace's mapping.
    m = build.Mapping(harness / "mapping.toml")
    for name, (since, until) in sorted(windows.items()):
        if name in excluded:
            continue
        lines = build.build(run, m, since, until)
        (traces / f"{name}.ndjson").write_text("".join(json.dumps(x) + "\n" for x in lines))

    (wd / "modeling-brief.md").write_text(
        BRIEF_HEAD + "".join(SCENARIOS[h] for h in HUNTS[args.variant]) + BRIEF_TAIL)
    (wd / ".prompt-extra.md").write_text(PROMPT_EXTRA.format(repo=REPO, webkit=REPO / "webkit", wd=wd))

    (harness / "rebuild_traces.sh").write_text(f"""#!/usr/bin/env bash
# Rebuild traces/ from the recorded runs with harness/mapping.toml (after a mapping change).
set -euo pipefail
python3 - <<'PY'
import json, sys
sys.path.insert(0, "{REPO / 'webkit' / 'tracebuilder'}")
import build
run = "{run}"
m = build.Mapping("{harness / 'mapping.toml'}")
for name, (since, until) in sorted(json.load(open(run + "/traces/windows.json")).items()):
    lines = build.build(__import__("pathlib").Path(run), m, since, until)
    open("{traces}/" + name + ".ndjson", "w").write("".join(json.dumps(x) + "\\n" for x in lines))
    print("rebuilt", name)
PY
""")
    (harness / "rebuild_traces.sh").chmod(0o755)
    (harness / "run.sh").write_text(f"""#!/usr/bin/env bash
# Record fresh seeded-shop traces through the webkit tracing stack.
set -euo pipefail
cd {REPO}
python3 webkit/stack/wkstack.py up --config eval/seeded-shop/stack.toml
python3 eval/seeded-shop/workloads.py --repeat 3
""")
    (harness / "run.sh").chmod(0o755)
    (harness / "INSTRUMENTATION.md").write_text(
        "No code instrumentation: the webkit stack records HTTP, SQL and committed changes at the app's\n"
        "boundaries. What each statement maps to is harness/mapping.toml; rebuild traces after changing it.\n")
    print(f"workspace ready ({args.variant}): {wd} ({len(list(traces.glob('*.ndjson')))} traces, "
          f"hunts: {', '.join(HUNTS[args.variant])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
