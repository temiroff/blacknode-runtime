#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
config="${BLACKNODE_RUNTIME_CONFIG:-$repo_dir/.blacknode-runtime/runtime.json}"
port="${BLACKNODE_RUNTIME_PORT:-8766}"
instance="${BLACKNODE_RUNTIME_INSTANCE:-}"
if [[ -n "$instance" && ! "$instance" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "BLACKNODE_RUNTIME_INSTANCE must contain lowercase letters, numbers, or hyphens."
  exit 2
fi
unit_name="blacknode-runtime${instance:+-$instance}.service"

usage() {
  echo "Usage: ./service.sh COMMAND"
  echo
  echo "Commands: overview, status, deployments, start, stop, restart, check, pairing, docker, logs, follow"
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
  overview)
    check_service --deployments
    echo
    hardware_dir="${BLACKNODE_HARDWARE_DIR:-$(cd -- "$repo_dir/.." && pwd)/blacknode-hardware}"
    if [[ -x "$hardware_dir/service.sh" ]]; then
      echo "Robot hardware services"
      echo "======================="
      if [[ -f "$hardware_dir/.blacknode-hardware/devices.json" ]]; then
        "$hardware_dir/service.sh" --all check
      else
        "$hardware_dir/service.sh" check
      fi
    else
      echo "Blacknode Hardware checkout was not found beside this runtime."
      echo "Set BLACKNODE_HARDWARE_DIR, then run ./service.sh overview again."
    fi
    ;;
  status) sudo systemctl --no-pager --full status "$unit_name" || true; echo; check_service --deployments ;;
  deployments) check_service --deployments ;;
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
  docker)
    "$repo_dir/setup-docker.sh"
    if systemctl is-active --quiet "$unit_name"; then
      check_service --wait 15
    fi
    ;;
  logs) sudo journalctl -u "$unit_name" -n 100 --no-pager ;;
  follow) sudo journalctl -u "$unit_name" -f ;;
  -h|--help|help) usage ;;
  *) echo "Unknown command: $command_name"; usage; exit 2 ;;
esac
