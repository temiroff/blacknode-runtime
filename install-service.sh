#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"
host="${BLACKNODE_RUNTIME_HOST:-0.0.0.0}"
port="${BLACKNODE_RUNTIME_PORT:-8766}"
instance="${BLACKNODE_RUNTIME_INSTANCE:-}"
if [[ -n "$instance" && ! "$instance" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "BLACKNODE_RUNTIME_INSTANCE must contain lowercase letters, numbers, or hyphens."
  exit 2
fi
unit_name="blacknode-runtime${instance:+-$instance}.service"
unit_path="/etc/systemd/system/$unit_name"
service_user="$(id -un)"
print_only=false

if [[ "${1:-}" == "--print" ]]; then
  print_only=true
  shift
fi
if (($#)); then
  echo "Usage: ./install-service.sh [--print]"
  exit 2
fi
if [[ "$(uname -s)" != "Linux" || "$(id -u)" -eq 0 ]]; then
  echo "Run this installer as your normal user on Ubuntu/Linux."
  exit 1
fi
if [[ ! -x "$python" || ! -f "$config" ]]; then
  echo "Blacknode Runtime is not ready. Run ./setup_ubuntu.sh first."
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is not available on this device."
  exit 1
fi

./check.sh
state_dir="$("$python" -c 'import sys; from pathlib import Path; from blacknode_runtime.config import RuntimeConfig; print(RuntimeConfig.load(Path(sys.argv[1])).state_dir)' "$config")"
token_file="$("$python" -c 'import sys; from pathlib import Path; from blacknode_runtime.config import RuntimeConfig; print(RuntimeConfig.load(Path(sys.argv[1])).auth_token_file)' "$config")"

unit_file="$(mktemp --suffix=.service)"
trap 'rm -f -- "$unit_file"' EXIT
"$python" "$repo_dir/scripts/render_systemd_unit.py" \
  --repo "$repo_dir" --user "$service_user" --host "$host" --port "$port" \
  --config "$config" --state-dir "$state_dir" > "$unit_file"

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$unit_file"
fi
if [[ "$print_only" == true ]]; then
  cat "$unit_file"
  exit 0
fi

sudo install -m 0644 "$unit_file" "${unit_path}.new"
sudo mv -f -- "${unit_path}.new" "$unit_path"
sudo systemctl daemon-reload
sudo systemctl enable "$unit_name"
sudo systemctl restart "$unit_name"

"$python" "$repo_dir/scripts/service_check.py" \
  --url "http://127.0.0.1:$port" --token-file "$token_file" --wait 15

if [[ "${BLACKNODE_CONFIGURE_UFW:-1}" != "0" ]]; then
  if command -v ufw >/dev/null 2>&1 \
    && sudo ufw status 2>/dev/null | grep -qi '^Status: active'; then
    echo "Allowing TCP port $port through UFW for Blacknode Runtime${instance:+ $instance}..."
    sudo ufw allow "$port/tcp" comment "Blacknode runtime${instance:+ $instance}"
  else
    echo "UFW is inactive or unavailable; no runtime firewall rule is needed."
  fi
fi

echo
echo "Blacknode Runtime${instance:+ $instance} installed and enabled at boot on port $port."
echo "Use ./service.sh status, restart, check, or logs."
