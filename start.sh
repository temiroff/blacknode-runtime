#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"
host="${BLACKNODE_RUNTIME_HOST:-0.0.0.0}"
port="${BLACKNODE_RUNTIME_PORT:-8766}"

if [[ ! -x "$python" ]]; then
  echo "Blacknode Runtime is not set up yet. Run ./setup_ubuntu.sh first."
  exit 1
fi
if [[ ! -f "$config" ]]; then
  echo "Blacknode Runtime is not configured. Run ./configure.sh first."
  exit 1
fi

echo "Starting Blacknode Runtime"
echo "Listening on http://$host:$port"
echo "Press Ctrl+C to stop."
exec "$python" "$repo_dir/scripts/runtime_service.py" \
  --host "$host" --port "$port" --config "$config"
