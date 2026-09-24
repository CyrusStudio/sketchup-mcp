#!/bin/bash
# Reinstall this checkout into the local venv and restart the stdio MCP server.
# Upstream pinned a PyPI release here; this fork installs the working tree, so a
# fix you just made is the thing you test.
set -euo pipefail

cd "$(dirname "$0")"

pkill -f "sketchup_mcp" || true

if [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "No .venv found. Run: uv venv && uv pip install -e '.[dev]'" >&2
  exit 1
fi

uv pip install --python "$PY" -e ".[dev]"
"$PY" -m pytest

echo
echo "Starting the MCP server on stdio (Ctrl-C to stop)."
exec "$PY" -m sketchup_mcp
