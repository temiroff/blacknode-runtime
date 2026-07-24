#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_user="$(id -un)"
restart_runtime=true

usage() {
  echo "Usage: ./setup-docker.sh [--no-restart]"
}

if [[ "${1:-}" == "--no-restart" ]]; then
  restart_runtime=false
  shift
fi
if (($#)); then
  usage
  exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Docker Engine setup is intended for Ubuntu/Linux."
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as your normal runtime user, not as root."
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required to manage Docker Engine on this device."
  exit 1
fi

echo "Blacknode Runtime Docker setup"
echo "=============================="

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine..."
  sudo apt-get update
  sudo apt-get install -y docker.io
fi

sudo systemctl enable --now docker.service

if ! id -nG "$runtime_user" | tr ' ' '\n' | grep -Fxq docker; then
  echo "Granting $runtime_user access to Docker..."
  sudo usermod -aG docker "$runtime_user"
fi

# sudo starts a fresh process with the user's current supplementary groups,
# so this validates socket access without requiring a logout/new login.
if ! sudo -u "$runtime_user" -H docker info >/dev/null 2>&1; then
  echo "Docker is running, but $runtime_user cannot access its socket."
  echo "Log out and back in once, then rerun ./service.sh docker."
  exit 1
fi

if [[ "$restart_runtime" == true ]] && systemctl list-unit-files blacknode-runtime.service --no-legend 2>/dev/null | grep -q '^blacknode-runtime.service'; then
  sudo systemctl restart blacknode-runtime.service
  echo "Blacknode Runtime restarted with Docker access."
fi

echo "[OK] Docker Engine is enabled at boot and available to $runtime_user."
echo "The first ROS deployment may take several minutes to build the rosbridge image."
