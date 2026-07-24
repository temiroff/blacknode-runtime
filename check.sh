#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"
export BLACKNODE_PACKAGE_PATH="${BLACKNODE_PACKAGE_PATH:-$repo_dir/packages}"

if [[ ! -x "$python" ]]; then
  echo "Blacknode Runtime is not set up yet. Run ./setup_ubuntu.sh first."
  exit 1
fi

exec "$python" "$repo_dir/scripts/runtime_doctor.py" --config "$config"
