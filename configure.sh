#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"

if [[ ! -x "$python" ]]; then
  echo "Blacknode Runtime is not set up yet. Run ./setup_ubuntu.sh first."
  exit 1
fi

args=(--config "$config")
if [[ "${1:-}" != "--show" && ! -f "$config" ]]; then
  token_file="${BLACKNODE_AUTH_TOKEN_FILE:-$repo_dir/../blacknode-hardware/.blacknode-hardware/auth.token}"
  args+=(--token-file "$token_file")
  if [[ -n "${BLACKNODE_CORE_PATH:-}" ]]; then
    args+=(--blacknode-root "$BLACKNODE_CORE_PATH")
  elif [[ -f "$repo_dir/../Blacknode/pyproject.toml" ]]; then
    args+=(--blacknode-root "$repo_dir/../Blacknode")
  fi
fi

exec "$python" "$repo_dir/scripts/configure_runtime.py" "${args[@]}" "$@"
