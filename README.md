# blacknode-runtime

Authenticated remote deployment and process supervision for Blacknode devices.

The runtime is separate from robot hardware and AI planning:

- `blacknode-runtime` stages versioned workflow scripts, starts and stops them,
  captures logs, reports the target environment, and rolls back revisions.
- `blacknode-hardware` owns physical device state and safe hardware commands.
- `blacknode-agent` owns planning, skills, confirmation, and memory.

## Install on Ubuntu, Raspberry Pi, or Jetson

For a new device, clone one repository and run the complete device installer:

```bash
git clone https://github.com/temiroff/blacknode-runtime.git
cd blacknode-runtime
./install-device.sh
```

The installer downloads `blacknode-hardware`, installs both Python
environments, discovers every responding serial robot, asks for friendly robot
names, creates one hardware service per robot, installs the shared deployment
runtime, configures active UFW rules, and prints the editor pairing checklist.
It is safe to rerun: existing robot identities, names, calibrations, and the
runtime token are preserved.

For an unattended two-robot setup:

```bash
./install-device.sh \
  --name "Leader" \
  --name "Follower" \
  --no-prompt
```

Preview the operations without changing the device:

```bash
./install-device.sh --plan
```

If a deployment is running, the installer makes no changes. Stop it in the
Blacknode editor, or explicitly authorize the installer to stop every running
deployment before continuing:

```bash
./install-device.sh --stop-deployments
```

Stopping a robot deployment may release actuator torque.

Manual runtime-only installation remains available. Clone it beside an
existing hardware repository:

```bash
git clone https://github.com/temiroff/blacknode-runtime.git
cd blacknode-runtime
./setup_ubuntu.sh
./install-service.sh
```

The runtime-only setup script creates `.venv`, installs Blacknode Runtime and Blacknode
core, finds the sibling hardware pairing token, saves local configuration, and
runs readiness checks. It never copies the token into Git or the runtime
configuration. Linux devices with ROS 2 installed use that native graph.
Foreground and systemd starts automatically source the installed
`/opt/ros/<distro>/setup.bash`, so deployed workflows and their driver
processes inherit `rclpy`, ROS topic discovery, and the configured ROS domain.

If the repositories are not siblings, provide the token path:

```bash
BLACKNODE_AUTH_TOKEN_FILE=/path/to/blacknode-hardware/.blacknode-hardware/auth.token \
./setup_ubuntu.sh
```

To install Blacknode core from a local checkout:

```bash
BLACKNODE_CORE_PATH=/path/to/Blacknode ./setup_ubuntu.sh
```

## Check and manage the service

```bash
./check.sh
./service.sh overview
./service.sh status
./service.sh deployments
./service.sh check
./service.sh pairing
./service.sh docker
./service.sh restart
./service.sh logs
./service.sh follow
```

`./service.sh overview` is the first command to run when the device state is
unclear. It shows runtime health, every deployment with its state, PID,
revision, and target robot, then checks each configured hardware service and
its USB identity. `./service.sh deployments` prints only the runtime and
deployment summary. `./service.sh status` includes the systemd service details
and the same deployment summary.

`./service.sh docker` is an optional fallback for container-backed workflows.
It installs Docker Engine when requested, enables it at boot, grants the
runtime user access to the Docker socket, and restarts the runtime service.
Native ROS 2 devices do not need this command.

The service listens on port `8766`. Allow it through Ubuntu's firewall once:

```bash
sudo ufw allow 8766/tcp
sudo ufw reload
```

Verify it from another computer:

```powershell
Test-NetConnection DEVICE_IP -Port 8766
```

The public endpoint is:

```text
http://DEVICE_IP:8766/health
```

`DEVICE_IP` is discovered on each Linux computer when the pairing checklist is
printed; it is not stored in the installer. Different computers can use
different DHCP addresses. For a long-lived robot, configure a DHCP reservation
in the router or use a resolvable hostname so the editor address remains stable.

`/manifest` and every deployment endpoint require the runtime pairing token
shown by `./service.sh pairing`. A single-device installation commonly shares
that token with the hardware service. Multi-robot hardware services may each
have a different hardware token, while port 8766 continues to use one shared
runtime token.
by the hardware service.

## Workflow package synchronization

The runtime needs every extension package that owns a node in the deployed
workflow. The Blacknode editor includes the required package sources in the
validated deployment plan. When the user selects **Send to device** or
**Send & run**,
the editor calls the authenticated package-sync endpoint before uploading the
workflow.

Package synchronization:

- clones only package sources declared by the workflow or Blacknode package
  index;
- installs the package's declared prerequisites into the runtime environment;
- activates the exact components and adapters declared by workflow metadata;
- loads its registered nodes;
- behaves idempotently when the requested version is already present;
- fast-forwards a clean package checkout when the editor requests a newer
  published version; and
- verifies package and node availability before the workflow is staged.

Manual package installation remains available for maintenance:

```bash
./install-package.sh blacknode-perception
```

For example, `Camera` is provided by `blacknode-perception`. Both automatic and
manual installation use the same `packages/` directory and Blacknode package
contracts. Package releases must bump `version` in `blacknode-package.toml`;
the editor sends that version and source to the runtime, so new package
releases do not require a runtime update.

## Runtime API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Public service identity |
| `GET` | `/manifest` | Python, platform, Blacknode, package, and feature inventory |
| `POST` | `/packages/sync` | Idempotently install packages and activate declared components and adapters |
| `GET` | `/deployments` | List staged and running deployments |
| `POST` | `/deployments` | Stage a Python deployment revision |
| `GET` | `/deployments/{id}` | Inspect one deployment |
| `POST` | `/deployments/{id}/start` | Explicitly start the staged revision |
| `POST` | `/deployments/{id}/stop` | Stop the complete deployment process group |
| `GET` | `/deployments/{id}/logs` | Read captured output |
| `POST` | `/deployments/{id}/rollback` | Select the previous revision, optionally start it |
| `DELETE` | `/deployments/{id}` | Delete a stopped deployment |

Staging and starting are separate operations. A staged workflow cannot move
hardware until it is explicitly started and then passes the hardware service's
own calibration, freshness, limits, and authorization checks.

Local state is stored under `.blacknode-runtime/` and excluded from Git.

## Security

The deployment API can execute authenticated Python artifacts. Keep port 8766
on a trusted private network. Pairing authentication controls access but plain
HTTP does not encrypt traffic; use a private VPN or HTTPS before crossing an
untrusted network.

## Development

```powershell
python -m pip install -e .
python -m pytest tests
```
