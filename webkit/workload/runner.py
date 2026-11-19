#!/usr/bin/env python3
"""Run an app's workloads through the tracing stack and build one model-level trace per run (generic).

    runner.py run     --stack stack.toml --mapping mapping.toml --workloads workloads.py --traces DIR
                      [--repeat N] [--only NAME ...] [--runs-dir DIR]
    runner.py rebuild --mapping mapping.toml --traces DIR [--out DIR]

`run` needs a running stack (wkstack.py ensure). For each scenario and repetition it resets the
database with the app's own seed command ([app].reset in the stack config), runs the scenario, waits
for the recorders to flush, and builds DIR/<scenario>-<n>.ndjson from that run's time window with the
mapping. DIR/windows.json records, per trace, the recording directory, the window, whether requests
were started concurrently, and the response statuses, so that

`rebuild` can rebuild every trace from the saved recordings after a mapping change, with no new
recording. (--out writes the rebuilt traces elsewhere, e.g. to compare them with DIR.)

The workloads file is the only app-specific input. It defines SCENARIOS, a dict from scenario name to
a function taking a Client:

    SCENARIOS = {
        "sequential": lambda c: [c.get("/items/1"), c.post("/orders", json={"itemId": 1})],
        "two_buyers": lambda c: c.together(
            lambda: c.post("/orders", json={"itemId": 1}, token="tok-a"),
            lambda: c.post("/orders", json={"itemId": 1}, token="tok-b")),
    }

Client.together starts its calls at the same moment (a barrier), so their SQL statements overlap;
a scenario that uses it is recorded as concurrent, and the coverage gate (tracecheck/coverage.py)
requires an overlapping-actors trace for it. Every request goes through the stack's inbound proxy,
which assigns each one its trace id.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, NamedTuple

WEBKIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEBKIT / "tracebuilder"))
import build  # noqa: E402

WKSTACK = WEBKIT / "stack" / "wkstack.py"
FLUSH_SECONDS = 1.0


class Response(NamedTuple):
    status: int
    body: Any          # parsed JSON when the response is JSON, else text
    headers: dict


class Client:
    """HTTP client for workloads; every request goes through the tracing stack's inbound proxy."""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.concurrent = False
        self.statuses: list[int] = []
        self._lock = threading.Lock()

    def request(self, method: str, path: str, *, json_body: Any = None, json: Any = None,
                data: bytes | None = None, headers: dict | None = None, token: str | None = None) -> Response:
        body = json if json is not None else json_body
        hdrs = dict(headers or {})
        if body is not None:
            data = _json_dumps(body)
            hdrs.setdefault("content-type", "application/json")
        if token is not None:
            hdrs.setdefault("authorization", f"Bearer {token}")
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = Response(r.status, _decode(r.read(), r.headers.get("content-type", "")), dict(r.headers))
        except urllib.error.HTTPError as e:
            resp = Response(e.code, _decode(e.read(), e.headers.get("content-type", "")), dict(e.headers))
        with self._lock:
            self.statuses.append(resp.status)
        return resp

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Response:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.request("DELETE", path, **kw)

    def together(self, *calls: Callable[[], Any]) -> list[Any]:
        """Start the calls at the same moment (a barrier) and wait for all; returns their results."""
        self.concurrent = True
        barrier = threading.Barrier(len(calls))
        out: list[Any] = [None] * len(calls)
        errors: list[BaseException] = []

        def run(i: int) -> None:
            barrier.wait()
            try:
                out[i] = calls[i]()
            except BaseException as e:  # noqa: BLE001 - reported after join
                errors.append(e)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        return out


def _json_dumps(value: Any) -> bytes:
    import json as _json
    return _json.dumps(value).encode()


def _decode(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", "replace")
    if "json" in content_type:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def load_workloads(path: Path) -> dict[str, Callable[[Client], Any]]:
    spec = importlib.util.spec_from_file_location("workloads", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load workloads from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["workloads"] = module
    spec.loader.exec_module(module)
    scenarios = getattr(module, "SCENARIOS", None)
    if not isinstance(scenarios, dict) or not scenarios:
        sys.exit(f"{path} must define SCENARIOS = {{name: function(client)}}")
    return scenarios


def wkstack(command: str, stack: Path, runs_dir: Path | None) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(WKSTACK), command, "--config", str(stack)]
    if runs_dir:
        argv += ["--runs-dir", str(runs_dir)]
    return subprocess.run(argv, capture_output=True, text=True)


def stack_state(stack: Path, runs_dir: Path | None) -> dict:
    p = wkstack("status", stack, runs_dir)
    if p.returncode != 0:
        sys.exit(f"wkstack status failed:\n{p.stdout}{p.stderr}")
    st = json.loads(p.stdout)
    if not st.get("running"):
        sys.exit("no running stack for this config; start it with wkstack.py ensure --config ...")
    return st


def load_windows(traces: Path) -> dict[str, dict]:
    f = traces / "windows.json"
    return json.loads(f.read_text()) if f.exists() else {}


def write_trace(out: Path, lines: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(x) + "\n" for x in lines))


def cmd_run(args: argparse.Namespace) -> int:
    with args.stack.open("rb") as f:
        tomllib.load(f)  # fail early on a broken config
    st = stack_state(args.stack, args.runs_dir)
    run_dir = Path(st["out_dir"])
    mapping = build.Mapping(args.mapping)
    scenarios = load_workloads(args.workloads)
    unknown = set(args.only or []) - set(scenarios)
    if unknown:
        sys.exit(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    args.traces.mkdir(parents=True, exist_ok=True)
    windows = load_windows(args.traces)
    failed = 0
    for name, scenario in scenarios.items():
        if args.only and name not in args.only:
            continue
        for n in range(1, args.repeat + 1):
            reset = wkstack("reset", args.stack, args.runs_dir)
            if reset.returncode != 0:
                sys.exit(f"database reset failed:\n{reset.stdout}{reset.stderr}")
            time.sleep(0.5)
            client = Client(st["base_url"])
            since = time.time_ns()
            try:
                scenario(client)
                scenario_error = ""
            except Exception as e:  # noqa: BLE001 - a broken workload is reported, not fatal for others
                scenario_error = f"{type(e).__name__}: {e}"
            time.sleep(FLUSH_SECONDS)
            until = time.time_ns()
            stem = f"{name}-{n}"
            windows[stem] = {"run_dir": str(run_dir), "since": since, "until": until, "scenario": name,
                             "concurrent": client.concurrent, "statuses": client.statuses}
            (args.traces / "windows.json").write_text(json.dumps(windows, indent=1))
            if scenario_error:
                failed += 1
                print(f"{stem}: WORKLOAD ERROR {scenario_error}")
                continue
            try:
                lines = build.build(run_dir, mapping, since, until)
            except build.BuildError as e:
                failed += 1
                print(f"{stem}: {e}")
                continue
            write_trace(args.traces / f"{stem}.ndjson", lines)
            print(f"{stem}.ndjson: {len(lines) - 1} events, {len(lines[0]['actors'])} actors, "
                  f"{'concurrent' if client.concurrent else 'sequential'}, statuses {client.statuses}")
    if failed:
        print(f"{failed} run(s) failed; fix the mapping or the workload and rerun (or rebuild)")
    return 1 if failed else 0


def window_of(entry: Any, fallback_run: Path | None) -> tuple[Path, int, int]:
    if isinstance(entry, list):                    # older format: [since, until]
        if fallback_run is None:
            sys.exit("windows.json uses the old [since, until] format; pass --run RUN_DIR")
        return fallback_run, int(entry[0]), int(entry[1])
    return Path(entry["run_dir"]), int(entry["since"]), int(entry["until"])


def cmd_rebuild(args: argparse.Namespace) -> int:
    windows = load_windows(args.traces)
    if not windows:
        sys.exit(f"no {args.traces / 'windows.json'}: nothing to rebuild")
    mapping = build.Mapping(args.mapping)
    out_dir = args.out or args.traces
    failed = 0
    for stem, entry in sorted(windows.items()):
        run_dir, since, until = window_of(entry, args.run)
        try:
            lines = build.build(run_dir, mapping, since, until)
        except build.BuildError as e:
            failed += 1
            print(f"{stem}: {e}")
            continue
        write_trace(out_dir / f"{stem}.ndjson", lines)
        print(f"{stem}.ndjson: {len(lines) - 1} events")
    if out_dir != args.traces and (args.traces / "windows.json").exists():
        (out_dir / "windows.json").write_text((args.traces / "windows.json").read_text())
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run workloads and build traces")
    r.add_argument("--stack", type=Path, required=True)
    r.add_argument("--mapping", type=Path, required=True)
    r.add_argument("--workloads", type=Path, required=True)
    r.add_argument("--traces", type=Path, required=True)
    r.add_argument("--repeat", type=int, default=3)
    r.add_argument("--only", nargs="*")
    r.add_argument("--runs-dir", type=Path)
    b = sub.add_parser("rebuild", help="rebuild traces from saved recordings")
    b.add_argument("--mapping", type=Path, required=True)
    b.add_argument("--traces", type=Path, required=True)
    b.add_argument("--out", type=Path)
    b.add_argument("--run", type=Path, help="recording dir for old-format windows.json entries")
    args = ap.parse_args()
    return cmd_run(args) if args.command == "run" else cmd_rebuild(args)


if __name__ == "__main__":
    sys.exit(main())
