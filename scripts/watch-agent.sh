#!/usr/bin/env bash
# Live, readable view of a running Specula agent (OpenCode activity stream).
# Usage: scripts/watch-agent.sh [path/to/agent.activity.jsonl]
# Default: the single-target output in the Specula checkout.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
file="${1:-$here/Specula/.specula-output/agent.activity.jsonl}"

if [[ ! -f "$file" ]]; then
  echo "No activity file at $file (has the run started?)" >&2
  exit 1
fi

# THINK = model reasoning, TOOL = a tool call (shell command or file path), SAY = model text.
tail -n +1 -F "$file" | jq --unbuffered -r '
  if .type == "tool_use" then
    "TOOL  \(.part.tool): \(.part.state.input.command // .part.state.input.filePath // .part.state.input.pattern // (.part.state.input | tostring))"
  elif .type == "reasoning" then
    "THINK \(.part.text | gsub("\n"; " ") | .[0:200])"
  elif .type == "text" then
    "SAY   \(.part.text | gsub("\n"; " ") | .[0:200])"
  else empty end'
