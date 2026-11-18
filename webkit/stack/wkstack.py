#!/usr/bin/env python3
"""Wrap any app's Docker Compose stack with the webkit tracing recorders.

The app's own compose file is never edited. This tool generates a compose
override from a small per-app stack config (TOML) and runs both together:

    wkstack.py up       --config stack.toml   start app + recorders, new run dir
    wkstack.py selftest --config stack.toml   check every signal source
    wkstack.py down     --config stack.toml   stop and remove containers
    wkstack.py overlay  --config stack.toml   print the generated override

Recorders added around the app (all output goes to runs/<run-id>/):
    wk-inbound  reverse proxy in front of the app      -> inbound.ndjson
    wk-pgproxy  PostgreSQL wire proxy (+ hold/release control API on host port 6433) -> pgproxy.ndjson
    wk-cdc      logical decoding of committed changes  -> cdc.ndjson
    wk-otelcol  OpenTelemetry collector                -> otel-spans.ndjson

Stack config (TOML):

    [app]
    compose = "path/to/compose.yaml"   # relative to the config file
    project = "seeded-shop"            # compose project name
    service = "app"                    # app service name
    port = 3000                        # port the app listens on inside its container
    runtime = "node"                   # "node" loads the OTel bootstrap; "none" skips it
    probe_path = "/products/1"         # GET path that touches the DB, for the self-test
    health_path = "/health"

    [app.env]                          # extra env for the app (optional)
    DB_POOL_SIZE = "2"

    [db]
    service = "db"
    kind = "postgres"
    port = 5432
    user = "shop"
    password = "shop"
    database = "shop"
    url_env = "DATABASE_URL"           # env var the app reads its DB URL from

    [inbound]
    host_port = 8080
    control_port = 6433                # pgproxy hold/release API (reproduction)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

WEBKIT = Path(__file__).resolve().parent.parent
REPO = WEBKIT.parent
RUNS = REPO / "runs"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.165.0"
RECORDER_IMAGE = "webkit-recorders:local"


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_dir"] = path.resolve().parent
    return cfg


def q(s: str) -> str:
    """Quote a string for YAML (JSON strings are valid YAML scalars)."""
    return json.dumps(s)


def overlay_yaml(cfg: dict, out_dir: Path) -> str:
    app, db = cfg["app"], cfg["db"]
    if db.get("kind", "postgres") != "postgres":
        sys.exit(f"unsupported db kind {db.get('kind')!r}: only postgres is implemented")
    inbound_port = cfg.get("inbound", {}).get("host_port", 8080)
    creds = f"{db['user']}:{db['password']}"
    proxied_url = f"postgresql://{creds}@wk-pgproxy:6432/{db['database']}"
    direct_url = f"postgresql://{creds}@{db['service']}:{db['port']}/{db['database']}"

    app_env = {db["url_env"]: proxied_url, **cfg["app"].get("env", {})}
    app_volumes = []
    if app.get("runtime", "none") == "node":
        app_env.update({
            "NODE_OPTIONS": "--import /opt/webkit/otel/register.mjs",
            "OTEL_SERVICE_NAME": app["service"],
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://wk-otelcol:4318",
            "OTEL_BSP_SCHEDULE_DELAY": "200",
        })
        app_volumes.append(f"{WEBKIT / 'otel' / 'node'}:/opt/webkit/otel:ro")

    out = str(out_dir)
    lines = ["services:"]
    lines += [
        f"  {db['service']}:",
        "    command: [\"postgres\", \"-c\", \"wal_level=logical\", \"-c\", \"max_replication_slots=10\", "
        "\"-c\", \"max_wal_senders=10\"]",
        f"  {app['service']}:",
        "    ports: !reset []",
        "    environment:",
        *[f"      {k}: {q(str(v))}" for k, v in app_env.items()],
    ]
    if app_volumes:
        lines += ["    volumes:", *[f"      - {q(v)}" for v in app_volumes]]
    lines += [
        "    depends_on:",
        "      wk-pgproxy: {condition: service_started}",
        "      wk-cdc: {condition: service_healthy}",
        "      wk-otelcol: {condition: service_started}",
    ]

    def recorder(name: str, binary: str, env: dict, extra: list[str] | None = None) -> list[str]:
        block = [
            f"  {name}:",
            f"    image: {RECORDER_IMAGE}",
            f"    build: {{context: {q(str(WEBKIT))}}}",
            f"    command: [\"{binary}\"]",
            "    environment:",
            *[f"      {k}: {q(str(v))}" for k, v in env.items()],
            "    volumes:",
            f"      - {q(out + ':/wk/out')}",
        ]
        return block + (extra or [])

    control_port = cfg.get("inbound", {}).get("control_port", 6433)
    lines += recorder("wk-pgproxy", "pgproxy", {"WK_UPSTREAM": f"{db['service']}:{db['port']}", "WK_DB_URL": direct_url},
                      ["    ports:", f"      - \"{control_port}:6433\"",
                       "    depends_on:", f"      {db['service']}: {{condition: service_healthy}}"])
    lines += recorder("wk-cdc", "cdc", {"WK_DB_URL": direct_url}, [
        "    depends_on:", f"      {db['service']}: {{condition: service_healthy}}",
        "    healthcheck:",
        "      test: [\"CMD\", \"grep\", \"-q\", \"\\\"ev\\\":\\\"ready\\\"\", \"/wk/out/cdc.ndjson\"]",
        "      interval: 1s",
        "      retries: 60",
    ])
    lines += recorder("wk-inbound", "inbound", {"WK_UPSTREAM": f"http://{app['service']}:{app['port']}"}, [
        "    ports:", f"      - \"{inbound_port}:8080\"",
    ])
    lines += [
        "  wk-otelcol:",
        f"    image: {COLLECTOR_IMAGE}",
        "    command: [\"--config=/etc/wk/otelcol.yaml\"]",
        "    user: \"0\"",
        "    volumes:",
        f"      - {q(str(WEBKIT / 'stack' / 'otelcol.yaml') + ':/etc/wk/otelcol.yaml:ro')}",
        f"      - {q(out + ':/wk/out')}",
    ]
    return "\n".join(lines) + "\n"


def compose_cmd(cfg: dict, overlay: Path) -> list[str]:
    app_compose = (cfg["_dir"] / cfg["app"]["compose"]).resolve()
    return ["docker", "compose", "-p", cfg["app"]["project"], "-f", str(app_compose), "-f", str(overlay)]


def state_file(cfg: dict) -> Path:
    return RUNS / f".current-{cfg['app']['project']}.json"


def cmd_up(cfg: dict, args: argparse.Namespace) -> None:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    if cfg["app"].get("runtime") == "node" and not (WEBKIT / "otel" / "node" / "node_modules").exists():
        print("+ npm ci (webkit/otel/node)", flush=True)
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=WEBKIT / "otel" / "node", check=True)
    out_dir = RUNS / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    overlay = out_dir / "compose.webkit.yaml"
    overlay.write_text(overlay_yaml(cfg, out_dir))
    cmd = compose_cmd(cfg, overlay) + ["up", "-d", "--build", "--wait"]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    state = {"run_id": run_id, "out_dir": str(out_dir), "overlay": str(overlay)}
    state_file(cfg).write_text(json.dumps(state, indent=2))
    base = f"http://localhost:{cfg.get('inbound', {}).get('host_port', 8080)}"
    wait_http(base + cfg["app"].get("health_path", "/"), timeout=120)
    print(f"up: run {run_id}; app via {base}; recordings in {out_dir}")


def current(cfg: dict) -> dict:
    p = state_file(cfg)
    if not p.exists():
        sys.exit("no running stack for this config; run `up` first")
    return json.loads(p.read_text())


def cmd_down(cfg: dict, _args: argparse.Namespace) -> None:
    if not state_file(cfg).exists():
        print(f"down: no running stack for project {cfg['app']['project']!r} (already stopped)")
        return
    st = current(cfg)
    subprocess.run(compose_cmd(cfg, Path(st["overlay"])) + ["down", "-v", "--remove-orphans"], check=True)
    state_file(cfg).unlink()
    print(f"down: recordings kept in {st['out_dir']}")


def wait_http(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    sys.exit(f"timed out waiting for {url}")


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def span_trace_ids(path: Path) -> set[str]:
    """Trace ids in the collector's file exporter output (OTLP JSON, one batch per line)."""
    ids: set[str] = set()
    for batch in read_events(path):
        for rs in batch.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    ids.add(span.get("traceId", ""))
    return ids


def cmd_selftest(cfg: dict, _args: argparse.Namespace) -> None:
    st = current(cfg)
    out = Path(st["out_dir"])
    base = f"http://localhost:{cfg.get('inbound', {}).get('host_port', 8080)}"
    trace_id = uuid.uuid4().hex
    req = urllib.request.Request(base + cfg["app"]["probe_path"],
                                 headers={"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"})
    with urllib.request.urlopen(req, timeout=10) as r:
        probe_status = r.status
    time.sleep(1.5)  # let the span exporter and file writers flush

    inbound = read_events(out / "inbound.ndjson")
    pgproxy = read_events(out / "pgproxy.ndjson")
    cdc = read_events(out / "cdc.ndjson")
    spans = span_trace_ids(out / "otel-spans.ndjson")

    checks = [
        ("inbound: probe request recorded",
         any(e.get("ev") == "response" and e.get("trace_id") == trace_id for e in inbound)),
        ("pgproxy: ready and app connected",
         any(e.get("ev") == "ready" for e in pgproxy) and any(e.get("ev") == "conn_open" for e in pgproxy)),
        ("pgproxy: probe SQL attributed to the probe request (sqlcommenter)",
         any(e.get("ev") == "stmt" and e.get("trace_id") == trace_id for e in pgproxy)),
        ("cdc: replication stream ready", any(e.get("ev") == "ready" for e in cdc)),
        ("cdc: committed transactions captured (migrations/seed)", any(e.get("ev") == "txn" for e in cdc)),
        ("otel: spans for the probe request exported", trace_id in spans),
    ]
    width = max(len(name) for name, _ in checks)
    print(f"selftest: run {st['run_id']}, probe GET {cfg['app']['probe_path']} -> {probe_status}")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
    if not all(ok for _, ok in checks):
        sys.exit(1)


def cmd_overlay(cfg: dict, _args: argparse.Namespace) -> None:
    print(overlay_yaml(cfg, RUNS / "<run-id>"), end="")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["up", "down", "selftest", "overlay"])
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--run-id")
    args = p.parse_args()
    cfg = load_config(args.config)
    RUNS.mkdir(exist_ok=True)
    {"up": cmd_up, "down": cmd_down, "selftest": cmd_selftest, "overlay": cmd_overlay}[args.command](cfg, args)


if __name__ == "__main__":
    os.environ.setdefault("COMPOSE_MENU", "false")
    main()
