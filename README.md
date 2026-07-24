# blacknode-runtime

Authenticated remote deployment and process supervision for Blacknode devices.

The runtime is separate from robot hardware and AI planning:

- `blacknode-runtime` stages versioned workflow scripts, starts and stops them,
  captures logs, reports the target environment, and rolls back revisions.
- `blacknode-hardware` owns physical device state and safe hardware commands.
- `blacknode-agent` owns planning, skills, confirmation, and memory.

## Install on Ubuntu, Raspberry Pi, or Jetson

Clone beside the existing hardware repository:

```bash
git clone https://github.com/temiroff/blacknode-runtime.git
cd blacknode-runtime
./setup_ubuntu.sh
./install-service.sh
```

The setup script creates `.venv`, installs Blacknode Runtime and Blacknode
core, finds the sibling hardware pairing token, saves local configuration, and
runs readiness checks. It never copies the token into Git or the runtime
configuration.

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
./service.sh status
./service.sh check
./service.sh restart
./service.sh logs
./service.sh follow
```

The service listens on port `8766`. Allow it through Ubuntu's firewall once:

```bash
sudo ufw allow 8766/tcp
sudo ufw reload
```

Verify it from another computer:

```powershell
Test-NetConnection 192.168.1.87 -Port 8766
```

The public endpoint is:

```text
http://192.168.1.87:8766/health
```

`/manifest` and every deployment endpoint require the same pairing token used
by the hardware service.

## Workflow package synchronization

The runtime needs every extension package that owns a node in the deployed
workflow. The Blacknode editor includes the required package sources in the
validated deployment plan. When the user selects **Stage** or **Stage & run**,
the editor calls the authenticated package-sync endpoint before uploading the
workflow.

Package synchronization:

- clones only package sources declared by the workflow or Blacknode package
  index;
- installs the package's declared prerequisites into the runtime environment;
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
| `POST` | `/packages/sync` | Idempotently install and load declared workflow packages |
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
