#!/usr/bin/env bash
set -euo pipefail

runtime_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
hardware_dir="${BLACKNODE_HARDWARE_DIR:-$(cd -- "$runtime_dir/.." && pwd)/blacknode-hardware}"
hardware_git_url="${BLACKNODE_HARDWARE_GIT_URL:-https://github.com/temiroff/blacknode-hardware.git}"
plan_only=false
stop_deployments=false
hardware_args=()

usage() {
  cat <<'EOF'
Install the complete Blacknode device stack on Ubuntu, Raspberry Pi, or Jetson.

Usage:
  ./install-device.sh [--plan] [--stop-deployments] [hardware discovery options]

Examples:
  ./install-device.sh
  ./install-device.sh --servos 6
  ./install-device.sh --name "Leader" --name "Follower" --no-prompt
  ./install-device.sh --stop-deployments
  ./install-device.sh --plan

The installer:
  1. installs blacknode-hardware and its dependencies;
  2. discovers and configures every responding serial robot;
  3. installs one hardware systemd service per robot;
  4. installs the shared blacknode-runtime service on port 8766;
  5. preserves an existing runtime pairing token on reruns; and
  6. prints the device overview and editor pairing checklist.

All unrecognized options are passed to hardware discovery. Physical motion
stays disarmed, and discovery reads servo positions without commanding motion.
The installer refuses to interrupt running deployments unless
--stop-deployments is explicitly provided. Stopping may release robot torque.

Environment:
  BLACKNODE_HARDWARE_DIR       Existing or desired hardware checkout path
  BLACKNODE_HARDWARE_GIT_URL   Hardware repository source
  BLACKNODE_CORE_PATH          Optional local Blacknode core checkout
  BLACKNODE_CONFIGURE_UFW=0    Skip automatic UFW rules
EOF
}

while (($#)); do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --plan)
      plan_only=true
      ;;
    --stop-deployments)
      stop_deployments=true
      ;;
    *)
      hardware_args+=("$1")
      ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run this installer as your normal user on Ubuntu/Linux."
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this installer as your normal user, not as root."
  exit 1
fi

echo "Blacknode device installation"
echo "============================="
echo "Runtime:  $runtime_dir"
echo "Hardware: $hardware_dir"
echo

if [[ "$plan_only" == true ]]; then
  echo "Plan"
  echo "----"
  if [[ -d "$hardware_dir" ]]; then
    echo "1. Reuse the existing blacknode-hardware checkout."
  else
    echo "1. Clone blacknode-hardware from $hardware_git_url."
  fi
  echo "2. Install hardware system and Python dependencies."
  echo "3. Discover every responding serial robot and install its service."
  echo "4. Reuse the configured runtime token, or share the first robot token."
  echo "5. Install and start blacknode-runtime on port 8766."
  echo "6. Check runtime, deployments, and every configured robot."
  exit 0
fi

if [[ ! -d "$hardware_dir" ]]; then
  echo "Installing Git so the hardware package can be downloaded..."
  sudo apt-get update
  sudo apt-get install -y git
  mkdir -p -- "$(dirname -- "$hardware_dir")"
  git clone "$hardware_git_url" "$hardware_dir"
elif [[ ! -f "$hardware_dir/blacknode-package.toml" ]]; then
  echo "The hardware path exists but is not a blacknode-hardware checkout:"
  echo "  $hardware_dir"
  exit 1
else
  echo "Using existing blacknode-hardware checkout."
fi

runtime_config="$runtime_dir/.blacknode-runtime/runtime.json"
runtime_token_file=""
if [[ -f "$runtime_config" ]]; then
  runtime_token_file="$(
    python3 - "$runtime_config" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(config.get("auth_token_file", ""))
PY
  )"
  if [[ -n "$runtime_token_file" && ! -f "$runtime_token_file" ]]; then
    echo "The existing runtime token file is missing:"
    echo "  $runtime_token_file"
    exit 1
  fi

  runtime_port="${BLACKNODE_RUNTIME_PORT:-8766}"
  stop_running=0
  if [[ "$stop_deployments" == true ]]; then
    stop_running=1
  fi
  if ! python3 - "$runtime_config" "$runtime_port" "$stop_running" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token_path = Path(config.get("auth_token_file", ""))
if not token_path.is_file():
    raise SystemExit(0)
token = token_path.read_text(encoding="utf-8").strip()
request = urllib.request.Request(
    f"http://127.0.0.1:{sys.argv[2]}/deployments",
    headers={"Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.loads(response.read())
except urllib.error.HTTPError as exc:
    print(f"Cannot inspect running deployments: runtime returned HTTP {exc.code}.")
    print("Repair the runtime pairing token before reinstalling device services.")
    raise SystemExit(1)
except (OSError, ValueError, urllib.error.URLError):
    raise SystemExit(0)
running = [
    item for item in payload.get("deployments", [])
    if isinstance(item, dict) and item.get("state") == "running"
]
if not running:
    raise SystemExit(0)
if sys.argv[3] == "1":
    for item in running:
        deployment_id = item.get("id")
        name = item.get("name") or deployment_id or "Deployment"
        if not deployment_id:
            print(f"Cannot stop {name}: its deployment ID is missing.")
            raise SystemExit(1)
        print(f"Stopping {name} before device installation...")
        stop_request = urllib.request.Request(
            f"http://127.0.0.1:{sys.argv[2]}/deployments/{deployment_id}/stop",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(stop_request, timeout=15) as response:
                json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"Could not stop {name}: {exc}")
            raise SystemExit(1)
    raise SystemExit(0)
print("Stop these deployments before reinstalling device services:")
for item in running:
    name = item.get("name") or item.get("id") or "Deployment"
    target = item.get("target_device_id") or "unassigned robot"
    print(f"  - {name} ({target})")
print("Then rerun this installer, or explicitly use:")
print("  ./install-device.sh --stop-deployments")
raise SystemExit(1)
PY
  then
    echo "No installation changes were made."
    exit 1
  fi
fi

echo
echo "Step 1/4 · Install hardware dependencies"
"$hardware_dir/setup_ubuntu.sh"

echo
echo "Step 2/4 · Discover, configure, and install every robot"
"$hardware_dir/configure.sh" --all --install "${hardware_args[@]}"

if [[ -z "$runtime_token_file" ]]; then
  mapfile -t device_rows < <(
    "$hardware_dir/.venv/bin/python" \
      "$hardware_dir/scripts/configure_devices.py" \
      --root "$hardware_dir/.blacknode-hardware" --list
  )
  if (( ${#device_rows[@]} == 0 )); then
    echo "No configured robot is available to provide the shared runtime token."
    exit 1
  fi
  IFS=$'\t' read -r _key _name _device_id _port _config runtime_token_file _unit \
    <<< "${device_rows[0]}"
  if [[ ! -f "$runtime_token_file" ]]; then
    echo "The first configured robot has no pairing token:"
    echo "  $runtime_token_file"
    exit 1
  fi
  echo "The shared runtime will reuse the first robot's pairing token."
else
  echo "Keeping the existing shared runtime pairing token."
fi

echo
echo "Step 3/4 · Install the shared deployment runtime"
BLACKNODE_AUTH_TOKEN_FILE="$runtime_token_file" "$runtime_dir/setup_ubuntu.sh"
"$runtime_dir/install-service.sh"

echo
echo "Step 4/4 · Verify the complete device"
"$runtime_dir/service.sh" overview

echo
echo "Editor pairing checklist"
echo "========================"
"$hardware_dir/pair.sh" --all --show
"$runtime_dir/service.sh" pairing

echo
echo "Installation complete."
echo "Add each listed robot in Blacknode Devices. Port 8766 is the shared runtime."
