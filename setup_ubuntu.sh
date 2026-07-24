#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

echo "Blacknode Runtime setup"
echo "======================="

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This setup script is intended for Ubuntu/Linux."
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as your normal user, not as root."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y git python3-pip python3-venv

python3 -m venv .venv
mkdir -p packages
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

core_path="${BLACKNODE_CORE_PATH:-}"
if [[ -z "$core_path" && -f "$repo_dir/../Blacknode/pyproject.toml" ]]; then
  core_path="$repo_dir/../Blacknode"
fi
if [[ -n "$core_path" ]]; then
  echo "Installing Blacknode core from $core_path..."
  python -m pip install -e "$core_path"
elif ! python -c "import blacknode" >/dev/null 2>&1; then
  echo "Installing Blacknode core..."
  python -m pip install "blacknode @ git+https://github.com/temiroff/Blacknode.git"
fi

token_file="${BLACKNODE_AUTH_TOKEN_FILE:-$repo_dir/../blacknode-hardware/.blacknode-hardware/auth.token}"
if [[ ! -f "$token_file" ]]; then
  echo
  echo "Pairing token not found at:"
  echo "  $token_file"
  echo "Run ./pair.sh in blacknode-hardware, then configure with:"
  echo "  BLACKNODE_AUTH_TOKEN_FILE=/path/to/auth.token ./configure.sh"
  exit 1
fi

configure_args=(--token-file "$token_file")
if [[ -n "$core_path" ]]; then
  configure_args+=(--blacknode-root "$core_path")
fi
./configure.sh "${configure_args[@]}"
./check.sh

echo
echo "Setup finished."
echo "For a foreground test:"
echo "  ./start.sh"
echo "For automatic startup:"
echo "  ./install-service.sh"
