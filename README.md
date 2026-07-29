# blacknode-runtime

Authenticated remote deployment and process supervision for Blacknode devices.

The runtime owns:

- process lifecycle and supervision;
- extension-component loading and dependency resolution;
- workflow staging and execution;
- authenticated configuration;
- logging;
- infrastructure deployment lifecycle: staging, start, stop, revision history,
  and rollback.

Deployment and rollback operate on runtime artifacts and supervised processes.
Robot behavior, motion planning, and hardware logic stay in their owning
packages.

Domain behavior remains with the package that owns it:

- `blacknode-runtime` stages versioned workflow scripts, starts and stops them,
  captures logs, reports the target environment, and rolls back revisions.
- `blacknode-robot` owns connected device state, lifecycle, health, telemetry,
  and safe hardware commands through its device components.
- `blacknode-agent` owns memory and executive mission orchestration.
- `blacknode-skills` owns reusable robot behavior.
- `blacknode-drivers` owns physical hardware communication.
- `blacknode-ros2` owns ROS graph and transport integration.

## Install on Ubuntu, Raspberry Pi, or Jetson

For a new device, clone one repository and run the complete device installer:

```bash
git clone https://github.com/temiroff/blacknode-runtime.git
cd blacknode-runtime
./install-device.sh
```

The installer downloads `blacknode-robot`, installs both Python
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
BLACKNODE_AUTH_TOKEN_FILE=/path/to/blacknode-robot/.blacknode-hardware/auth.token \
./setup_ubuntu.sh
```

To install Blacknode core from a local checkout:

```bash
BLACKNODE_CORE_PATH=/path/to/Blacknode ./setup_ubuntu.sh
```

Independent runtime instances use a separate checkout and pairing token, plus
an instance name and port. The instance name produces a distinct systemd unit
such as `blacknode-runtime-instance-2.service`:

```bash
BLACKNODE_AUTH_TOKEN_FILE="$HOME/.blacknode/runtimes/instance-2.auth.token" \
BLACKNODE_RUNTIME_INSTANCE=instance-2 \
BLACKNODE_RUNTIME_PORT=8767 \
./setup_ubuntu.sh

BLACKNODE_RUNTIME_INSTANCE=instance-2 \
BLACKNODE_RUNTIME_PORT=8767 \
./install-service.sh
```

Pass the same `BLACKNODE_RUNTIME_INSTANCE` and `BLACKNODE_RUNTIME_PORT` values
to `service.sh` when managing that instance. The Blacknode editor’s automatic
SSH setup allocates these values and checks occupied ports for you.
Choose **Install a complete isolated robot stack** in the editor when the
instance also needs its own `blacknode-robot` checkout, environment,
configuration, calibration state, tokens, service namespace, and ports. The
new Hardware environment starts empty; connect a new robot and explicitly add
its stable serial path from the isolated device card.

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
- repairs missing colcon overlays declared by enabled `ros2-workspaces`;
- sources built, manifest-declared ROS 2 workspaces for managed services and
  deployments;
- loads its registered nodes;
- behaves idempotently when the requested version is already present;
- preserves tracked and untracked device-local package edits in a recoverable
  Git stash before an automatic update;
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

Runtime-managed package updates preserve a modified checkout before pulling.
The synchronization messages report the stash reference and a recovery command.
Blacknode leaves the stash unapplied so the updated package stays reproducible
and deployment preflight can continue. Operators can inspect or restore those
edits later with `git -C packages/<name> stash list` and `stash pop`.

ROS 2 package adapters declare their workspace roots in the owning component or
adapter:

```toml
[components.camera.adapters.ros2]
ros2-workspaces = ["components/camera/adapters/ros2/ros2_ws"]
```

Each path is relative to the extension-package root. Synchronization verifies
`<workspace>/install/setup.bash`; when it is missing, the Runtime reruns the
package prerequisites and setup hooks. Synchronization stops with the missing
overlay paths when package setup cannot build them. This contract applies to
every extension package and keeps optional, disabled adapters isolated.

## Runtime API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Public service identity |
| `GET` | `/manifest` | Python, platform, Blacknode, package, and feature inventory |
| `POST` | `/packages/sync` | Install packages, activate components/adapters, and prepare declared ROS 2 workspaces |
| `GET` | `/deployments` | List staged and running deployments |
| `POST` | `/deployments` | Stage a Python deployment revision |
| `GET` | `/deployments/{id}` | Inspect one deployment |
| `POST` | `/deployments/{id}/start` | Explicitly start the staged revision |
| `POST` | `/deployments/{id}/stop` | Stop the complete deployment process group |
| `GET` | `/deployments/{id}/logs` | Read captured output |
| `GET` | `/deployments/{id}/telemetry` | Read the latest normalized telemetry sample from the running deployment |
| `GET` | `/deployments/{id}/workflow` | Load the typed graph captured for the active or staged revision |
| `POST` | `/deployments/{id}/control` | Explicitly arm or disarm one declared deployment motion gate |
| `POST` | `/deployments/{id}/rollback` | Select the previous revision, optionally start it |
| `DELETE` | `/deployments/{id}` | Delete a stopped deployment |
| `GET` | `/diagnostics/ros2` | Read ROS 2 nodes, topics, services, and robot-topic endpoint details |
| `GET` | `/services` | List managed attachment/provider services independently from deployments |
| `GET` | `/services/{id}` | Inspect one service and its declared ROS 2 interface readiness |
| `POST` | `/services/{id}/start` | Idempotently start a vetted `ros2 run` or `ros2 launch` provider |
| `POST` | `/services/{id}/stop` | Stop only that provider process group |
| `GET` | `/services/{id}/logs` | Read provider output |

Staging and starting are separate operations. A staged workflow cannot move
hardware until it is explicitly started and then passes the hardware service's
own calibration, freshness, limits, and authorization checks.

Each target robot has one running deployment owner. Starting a staged
deployment stops the complete process group of any other running deployment
whose manifest names the same `target_device_id`. The start response records
those deployment IDs in `superseded_deployment_ids`, allowing the editor to
remove their stopped records after the replacement starts successfully.

Deployment manifests can include `project_id` and `workflow_slug`. The runtime
persists both values on the deployment record and exposes them from every
deployment endpoint. Existing records without these fields remain valid and
are reported with empty values. Staging another revision preserves existing
ownership when the fields are omitted and rejects attempts to move an owned
deployment to another project or workflow.

Local state is stored under `.blacknode-runtime/` and excluded from Git.

### Deployment telemetry

Runtime 0.3.9 opens a loopback-only UDP receiver for each running deployment
and passes its address plus a random per-run token through process environment
variables. Compatible drivers publish normalized, best-effort state to that
receiver. The runtime retains only the latest sample for each stream and
exposes it through the authenticated deployment API; it does not persist
telemetry or accept control messages on this channel.

The `robot-state` stream carries joint position and velocity, optional joint
limits, connection and torque state, units, and driver errors. Monitor charts
use valid joint limits as their stable physical scale. Future capability
providers can add battery and camera-stream metadata through the same envelope.
Image frames do not belong in this UDP channel; camera providers should publish
a managed stream handle instead.

Runtime 0.3.11 supervises deployments whose manifest sets
`telemetry_required=true`. A robot deployment gets a bounded startup grace
period to publish its first state. Missing or stale state fails the deployment
and terminates its complete process group, allowing the Hardware service to
reclaim the device instead of leaving a stale deployment marked running.
Compute-only deployments do not require robot telemetry.

Runtime 0.3.12 serializes deployment ownership per `target_device_id`. Starting
a replacement stops every older process group for that robot before launching
the new revision.

Runtime 0.3.13 stores the typed workflow snapshot beside every staged revision.
The workflow endpoint also safely recovers the `_WORKFLOW` literal embedded in
generated scripts from earlier Runtime versions, so those deployments can be
opened as editable graphs again.

Runtime 0.3.14 includes endpoint counts for every discovered ROS 2 topic in
the authenticated diagnostics snapshot. Topic inspection is read-only and
bounded so larger graphs remain responsive. Discovered node names are checked
against live topic endpoints and service ownership so destroyed helper nodes
retained by the ROS CLI cache are not reported as active.

Runtime 0.3.15 lets an authenticated package sync explicitly fast-forward
already-installed extension packages even when their declared version has not
changed. Managed Runtime updates use this to refresh workflow packages before
the device returns online, so restarted deployments do not retain old package
code under a current Runtime service.

ROS 2 diagnostics are authenticated and read-only. They run a fixed set of
discovery commands and never publish a message or expose a general-purpose
remote shell.

Deployment motion control accepts only `arm` and `disarm`. It publishes to the
fixed controller topic declared by the staged workflow, rejects deployments
with no declared gate or multiple gates, and resets to disarmed whenever the
deployment stops, exits, fails, or the Runtime restarts.

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
