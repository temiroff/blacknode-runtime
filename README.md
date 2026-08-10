# blacknode-runtime

`blacknode-runtime` is the authenticated device agent for staging, starting, stopping, supervising, updating, and rolling back Blacknode deployments.

## Operator workflow

Use the Blacknode editor for routine device operations:

1. Open **Devices** and select the device.
2. Use **Software → Check updates** to update Runtime or extension packages.
3. Use the Runtime card to restart the service when required.
4. Send validated workflows through **Send to device** or **Send & run**.
5. Inspect deployment state, logs, telemetry, and rollback from the editor.

A deployment is staged before it is started. Starting is a separate explicit operation, and one target robot has one running deployment owner.

Enabling Runtime does not disable, replace, or remove a robot's existing ROS 2
bringup. Managed deployments and attachment services are not resumed after a
Runtime restart or device reboot; the operator starts them explicitly again.
Runtime shutdown terminates its complete process control group, while vendor
services outside that group continue under their original boot configuration.

## Device installation

For a new Ubuntu, Raspberry Pi, or Jetson device:

```bash
git clone https://github.com/temiroff/blacknode-runtime.git
cd blacknode-runtime
./install-device.sh
```

The installer prepares Runtime, discovers supported serial robots, configures services and firewall rules, and prints the editor pairing checklist. `./install-device.sh --plan` previews changes. It refuses to update while deployments are running unless stopping them is explicitly authorized.

## Deployment contract

The authenticated API supports package synchronization, artifact staging, start/stop, logs, latest normalized telemetry, workflow retrieval, remote ROS 2 topic streams, arm/disarm of one declared motion gate, persistent mapping controls, revision rollback, and managed provider services. A deployed `MapEnvironment` declares its map topic and save services in the deployment manifest, allowing the editor to show the live occupancy grid and save a map while the mapping process remains owned by that deployment. Package synchronization installs only declared sources/components and verifies node and ROS workspace availability before staging.

Local runtime state is stored under `.blacknode-runtime/` and excluded from Git. Pairing tokens are never written to logs, process arguments, workflow artifacts, or repository files.

## Security and recovery

- Keep port 8766 on a trusted private network or behind HTTPS/VPN.
- Staging never starts a deployment.
- Stop terminates the complete deployment process group and is idempotent.
- Previous revisions remain available for explicit rollback.
- Stale required telemetry fails and stops the deployment.

Shell helpers such as `service.sh`, `check.sh`, and `install-package.sh` are recovery and maintenance paths when the editor action is unavailable.

## Development

```powershell
python -m pip install -e .
python -m pytest tests
```

See [AGENTS.md](AGENTS.md) for authentication, staging, and rollback rules.
