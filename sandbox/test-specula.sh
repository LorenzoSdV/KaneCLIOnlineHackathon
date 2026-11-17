#!/usr/bin/env bash
# Run Specula's test suite inside the Linux sandbox container, the way its CI does.
# The checkout is mounted read-only and copied in, so the run cannot modify it.
# Usage: sandbox/test-specula.sh [extra pytest args...]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
specula="$(cd "$here/../Specula" && pwd)"
image="specula-sandbox:latest"
log_dir="$here/logs"
mkdir -p "$log_dir"
log="$log_dir/specula-tests-$(date +%Y%m%d-%H%M%S).log"

docker build -t "$image" "$here"

echo "Running tests; log: $log"
docker run --rm \
  --name specula-tests \
  --cpus 4 \
  -v "$specula:/src:ro" \
  "$image" \
  bash -c '
    set -euo pipefail
    rsync -a --exclude .venv --exclude .specula-output --exclude runs /src/ /home/specula/Specula/
    cd /home/specula/Specula
    uv sync --locked --extra dev
    uv run python scripts/infra/bundle_lite.py --check
    PYTHONPATH=tools uv run pytest tools/inv_checking_tool/tests -q
    PYTHONPATH=tools/trace_debugger/src uv run python tools/trace_debugger/tests/test_basic.py
    uv run pytest tests/ -q -n 4 --dist worksteal --durations=20 -p no:cacheprovider "$@"
  ' _ "$@" 2>&1 | tee "$log"
