#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$repo_dir/.venv/bin/python"
packages_dir="$repo_dir/packages"
source_name="${1:-}"

if [[ -z "$source_name" || "${2:-}" != "" ]]; then
  echo "Usage: ./install-package.sh PACKAGE"
  echo
  echo "Examples:"
  echo "  ./install-package.sh blacknode-perception"
  echo "  ./install-package.sh https://github.com/temiroff/blacknode-perception.git"
  exit 2
fi
if [[ ! -x "$python" ]]; then
  echo "Blacknode Runtime is not set up yet. Run ./setup_ubuntu.sh first."
  exit 1
fi

mkdir -p "$packages_dir"
export BLACKNODE_PACKAGE_PATH="$packages_dir"

clean_source="${source_name%/}"
clean_source="${clean_source%.git}"
package_name="${clean_source##*/}"
package_name="${package_name##*:}"
package_dir="$packages_dir/$package_name"

echo "Blacknode Runtime package setup"
echo "================================"
if [[ -d "$package_dir/.git" ]]; then
  echo "Updating $package_name..."
  git -C "$package_dir" pull --ff-only
  "$python" -m blacknode.cli packages setup "$package_name" \
    --directory "$packages_dir"
elif [[ -e "$package_dir" ]]; then
  echo "$package_dir already exists but is not a Git package checkout."
  exit 1
else
  "$python" -m blacknode.cli packages install "$source_name" \
    --directory "$packages_dir"
fi

echo
if systemctl list-unit-files blacknode-runtime.service >/dev/null 2>&1; then
  echo "Updating and restarting the Blacknode Runtime service..."
  "$repo_dir/install-service.sh"
else
  echo "Package installed. Start the runtime with ./start.sh,"
  echo "or install it at boot with ./install-service.sh."
fi
