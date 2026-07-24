#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"
port="${BLACKNODE_RUNTIME_PORT:-8766}"
unit_name="blacknode-runtime.service"

usage() {
  echo "Usage: ./service.sh COMMAND"
  echo
  echo "Commands: status, start, stop, restart, check, pairing, logs, follow"
}

if [[ ! -x "$python" || ! -f "$config" ]]; then
  echo "Blacknode Runtime is not ready. Run ./setup_ubuntu.sh first."
  exit 1
fi
command_name="${1:-}"
[[ -n "$command_name" ]] || { usage; exit 2; }
shift
token_file="$("$python" -c 'import sys; from pathlib import Path; from blacknode_runtime.config import RuntimeConfig; print(RuntimeConfig.load(Path(sys.argv[1])).auth_token_file)' "$config")"

check_service() {
  "$python" "$repo_dir/scripts/service_check.py" \
    --url "http://127.0.0.1:$port" --token-file "$token_file" "$@"
}

case "$command_name" in
  status) sudo systemctl --no-pager --full status "$unit_name" || true; echo; check_service ;;
  start) sudo systemctl start "$unit_name"; check_service --wait 15 ;;
  stop) sudo systemctl stop "$unit_name"; echo "Blacknode Runtime stopped." ;;
  restart) sudo systemctl restart "$unit_name"; check_service --wait 15 ;;
  check) check_service "$@" ;;
  pairing)
    device_ip="${BLACKNODE_DEVICE_IP:-}"
    if [[ -z "$device_ip" ]]; then
      device_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    device_ip="${device_ip:-DEVICE_IP}"
    "$python" "$repo_dir/scripts/show_pairing.py" \
      --config "$config" --url "http://$device_ip:$port"
    ;;
  logs) sudo journalctl -u "$unit_name" -n 100 --no-pager ;;
  follow) sudo journalctl -u "$unit_name" -f ;;
  -h|--help|help) usage ;;
  *) echo "Unknown command: $command_name"; usage; exit 2 ;;
esac
