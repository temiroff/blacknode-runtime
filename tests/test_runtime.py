from __future__ import annotations

import json
import os
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from blacknode_runtime.auth import load_auth_token, token_fingerprint
from blacknode_runtime.config import RuntimeConfig
from blacknode_runtime.deployments import DeploymentError, DeploymentStore
from blacknode_runtime.manifest import FEATURES, runtime_manifest
from blacknode_runtime.package_manager import PackageManager, PackageSyncError
from blacknode_runtime.server import create_server
from blacknode_runtime.telemetry import DeploymentTelemetryPublisher, DeploymentTelemetryReceiver
from scripts.render_systemd_unit import render_unit
from scripts.service_check import print_deployments
from scripts.show_pairing import main as show_pairing


def _config(tmp_path: Path, token: str = "x" * 48) -> tuple[RuntimeConfig, Path]:
    token_path = tmp_path / "auth.token"
    token_path.write_text(token + "\n", encoding="utf-8")
    config = RuntimeConfig.from_values(
        device_id="pi-test",
        auth_token_file=str(token_path),
        state_dir=str(tmp_path / "state"),
        hardware_url="http://127.0.0.1:8765",
    )
    return config, token_path


def _request(url: str, *, token: str | None = None, payload: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read())


def test_config_round_trip_contains_token_path_not_secret(tmp_path: Path):
    config, token_path = _config(tmp_path)
    config_path = config.save(tmp_path / "runtime.json")
    loaded = RuntimeConfig.load(config_path)
    assert loaded == config
    assert Path(config.state_dir).is_dir()
    assert load_auth_token(token_path) == "x" * 48
    assert "x" * 48 not in config_path.read_text(encoding="utf-8")


def test_pairing_command_displays_runtime_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    token = "runtime-token-" + "x" * 32
    config, _ = _config(tmp_path, token)
    config_path = config.save(tmp_path / "runtime.json")
    monkeypatch.setattr(sys, "argv", [
        "show_pairing.py",
        "--config",
        str(config_path),
        "--url",
        "http://192.168.1.87:8766",
    ])

    assert show_pairing() == 0
    output = capsys.readouterr().out
    assert "http://192.168.1.87:8766" in output
    assert f"Fingerprint: {token_fingerprint(token)}" in output
    assert f"Runtime token: {token}" in output


def test_manifest_reports_real_runtime_features(tmp_path: Path):
    config, _ = _config(tmp_path)
    manifest = runtime_manifest(config)
    assert manifest["service"] == "blacknode-runtime"
    assert manifest["protocol_version"] == 1
    assert set(FEATURES) <= set(manifest["features"])
    assert manifest["python"]["version"]
    assert manifest["device_id"] == "pi-test"
    assert isinstance(manifest["node_types"], list)


def test_runtime_check_prints_deployment_owner_and_process(capsys):
    print_deployments({
        "deployments": [{
            "id": "leader-live",
            "name": "Leader live",
            "state": "running",
            "target_device_id": "leader-31481",
            "project_id": "leader-follower-demo",
            "workflow_slug": "leader-deploy",
            "pid": 4321,
            "active_revision": "cafebabecafebabe",
            "updated_at": "2026-07-24T23:00:00+00:00",
            "error": "",
        }],
    })

    output = capsys.readouterr().out
    assert "1 total · 1 running" in output
    assert "[RUNNING] Leader live" in output
    assert "Target robot: leader-31481" in output
    assert "Project: leader-follower-demo" in output
    assert "Workflow: leader-deploy" in output
    assert "PID: 4321" in output
    assert "Revision: cafebabecafebabe" in output


def test_manifest_reports_packages_from_runtime_package_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    packages_dir = tmp_path / "packages"
    package_dir = packages_dir / "blacknode-example"
    package_dir.mkdir(parents=True)
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-example"\n'
        'version = "1.2.3"\n'
        'description = "Example"\n'
        'requires-blacknode = ">=0.3.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLACKNODE_PACKAGE_PATH", str(packages_dir))
    config, _ = _config(tmp_path)

    packages = {
        item["name"]: item
        for item in runtime_manifest(config)["packages"]
    }

    assert packages["blacknode-example"]["version"] == "1.2.3"
    assert packages["blacknode-example"]["source"] == "workspace"


def test_blacknode_package_manifest_loads():
    path = Path(__file__).resolve().parents[1] / "blacknode-package.toml"
    package = tomllib.loads(path.read_text(encoding="utf-8"))["package"]
    assert package["name"] == "blacknode-runtime"
    assert package["layer"] == "runtime"
    assert package["component-mode"] is True


def test_package_sync_rejects_source_repository_name_mismatch(tmp_path: Path):
    manager = PackageManager(tmp_path / "packages")
    with pytest.raises(PackageSyncError, match="repository name"):
        manager.sync({
            "packages": [{
                "name": "blacknode-perception",
                "git_url": "https://github.com/temiroff/not-perception.git",
            }],
        })


def test_package_sync_activates_declared_components_and_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import blacknode.packages as packages

    manager = PackageManager(tmp_path / "packages")
    package_dir = manager.root / "blacknode-skills"
    package_dir.mkdir()
    info = SimpleNamespace(
        name="blacknode-skills",
        version="0.1.0",
        node_types=["ROS2LeaderFollower"],
    )
    monkeypatch.setattr(
        manager,
        "_load_existing",
        lambda _spec, _path, _messages: info,
    )
    activated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        packages,
        "ensure_component_enabled",
        lambda package, component, **_kwargs: (
            activated.append(("component", package, component)) or info
        ),
    )
    monkeypatch.setattr(
        packages,
        "ensure_adapter_enabled",
        lambda package, component, adapter, **_kwargs: (
            activated.append(("adapter", package, component, adapter)) or info
        ),
    )

    result = manager.sync({
        "packages": [{
            "name": "blacknode-skills",
            "git_url": "https://github.com/temiroff/blacknode-skills.git",
            "version": "0.1.0",
            "components": ["follow-person"],
            "adapters": [{
                "component": "follow-person",
                "adapter": "ros2",
            }],
        }],
    })

    assert activated == [
        ("component", "blacknode-skills", "follow-person"),
        ("adapter", "blacknode-skills", "follow-person", "ros2"),
    ]
    assert result["activated"] == [
        {
            "package": "blacknode-skills",
            "component": "follow-person",
            "adapter": "",
        },
        {
            "package": "blacknode-skills",
            "component": "follow-person",
            "adapter": "ros2",
        },
    ]


def test_stage_start_logs_and_revision_rollback(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    first = store.stage({
        "name": "Example",
        "deployment_id": "example",
        "script": "print('revision one')\n",
        "manifest": {"schema_version": 1, "target_device_id": "leader-device"},
    })
    assert first["target_device_id"] == "leader-device"
    running = store.start(first["id"])
    assert running["state"] == "running"
    for _ in range(50):
        finished = store.get(first["id"])
        if finished["state"] != "running":
            break
        time.sleep(0.02)
    assert finished["state"] == "exited"
    assert "revision one" in store.logs(first["id"])

    second = store.stage({
        "name": "Example",
        "deployment_id": "example",
        "script": "print('revision two')\n",
        "manifest": {"schema_version": 1},
    })
    assert second["target_device_id"] == "leader-device"
    assert len(second["revisions"]) == 2
    second = store.start("example")
    for _ in range(50):
        second = store.get("example")
        if second["state"] != "running":
            break
        time.sleep(0.02)
    rolled_back = store.rollback("example")
    assert rolled_back["staged_revision"] == first["staged_revision"]


def test_start_replaces_every_running_deployment_for_same_target(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    first = store.stage({
        "name": "Follower old",
        "deployment_id": "follower-old",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {"schema_version": 1, "target_device_id": "follower-device"},
    })
    unrelated = store.stage({
        "name": "Leader",
        "deployment_id": "leader",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {"schema_version": 1, "target_device_id": "leader-device"},
    })
    replacement = store.stage({
        "name": "Follower replacement",
        "deployment_id": "follower-replacement",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {"schema_version": 1, "target_device_id": "follower-device"},
    })
    store.start(first["id"])
    store.start(unrelated["id"])
    try:
        running = store.start(replacement["id"])

        assert running["state"] == "running"
        assert running["superseded_deployment_ids"] == ["follower-old"]
        assert store.get(first["id"])["state"] == "stopped"
        assert store.get(unrelated["id"])["state"] == "running"
    finally:
        store.stop_all()


def test_deployment_telemetry_bridge_reports_latest_robot_state():
    receiver = DeploymentTelemetryReceiver("leader-live", stale_seconds=1)
    receiver.start()
    publisher = DeploymentTelemetryPublisher.from_env({
        **receiver.environment(),
        "BLACKNODE_DEPLOYMENT_ID": "leader-live",
    })
    try:
        assert publisher.enabled is True
        assert publisher.publish_robot_state(
            {"shoulder": 12.5, "elbow": -4.0},
            torque_enabled=True,
        )
        for _ in range(20):
            sample = receiver.latest()
            if sample["available"]:
                break
            time.sleep(0.01)
        assert sample["available"] is True
        assert sample["stale"] is False
        assert sample["payload"]["torque_enabled"] is True
        assert sample["payload"]["joints"] == [
            {"name": "shoulder", "position": 12.5, "velocity": 0.0},
            {"name": "elbow", "position": -4.0, "velocity": 0.0},
        ]
    finally:
        publisher.close()
        receiver.close()


def test_running_deployment_can_publish_telemetry_to_store(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    script = """
import json, os, socket, time
host, port = os.environ["BLACKNODE_TELEMETRY_UDP"].rsplit(":", 1)
message = {
    "protocol_version": 1,
    "token": os.environ["BLACKNODE_TELEMETRY_TOKEN"],
    "deployment_id": os.environ["BLACKNODE_DEPLOYMENT_ID"],
    "stream": "robot-state",
    "sequence": 1,
    "sent_at": "2026-07-25T00:00:00+00:00",
    "payload": {
        "connected": True,
        "torque_enabled": False,
        "position_unit": "degree",
        "velocity_unit": "degree/s",
        "joints": [{"name": "gripper", "position": 7.0, "velocity": 0.0}],
    },
}
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(json.dumps(message).encode(), (host, int(port)))
time.sleep(1)
"""
    staged = store.stage({
        "name": "Telemetry",
        "deployment_id": "telemetry",
        "script": script,
    })
    store.start(staged["id"])
    try:
        for _ in range(50):
            sample = store.telemetry(staged["id"])
            if sample["available"]:
                break
            time.sleep(0.02)
        assert sample["available"] is True
        assert sample["state"] == "running"
        assert sample["payload"]["joints"][0]["name"] == "gripper"
    finally:
        store.stop_all()


def test_required_robot_telemetry_must_start_or_deployment_fails(tmp_path: Path):
    store = DeploymentStore(
        tmp_path / "deployments",
        telemetry_startup_grace_seconds=0.12,
        telemetry_stale_failure_seconds=0.12,
        telemetry_watchdog_interval_seconds=0.02,
    )
    staged = store.stage({
        "name": "Missing telemetry",
        "deployment_id": "missing-telemetry",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {
            "schema_version": 1,
            "telemetry_required": True,
        },
    })
    store.start(staged["id"])
    try:
        for _ in range(100):
            result = store.get(staged["id"])
            if result["state"] != "running":
                break
            time.sleep(0.02)
        assert result["state"] == "failed"
        assert result["pid"] is None
        assert result["exit_code"] is not None
        assert "telemetry did not start" in result["error"]
    finally:
        store.stop_all()


def test_required_robot_telemetry_must_remain_fresh(tmp_path: Path):
    store = DeploymentStore(
        tmp_path / "deployments",
        telemetry_startup_grace_seconds=0.5,
        telemetry_stale_failure_seconds=0.12,
        telemetry_watchdog_interval_seconds=0.02,
    )
    script = """
import json, os, socket, time
host, port = os.environ["BLACKNODE_TELEMETRY_UDP"].rsplit(":", 1)
message = {
    "protocol_version": 1,
    "token": os.environ["BLACKNODE_TELEMETRY_TOKEN"],
    "deployment_id": os.environ["BLACKNODE_DEPLOYMENT_ID"],
    "stream": "robot-state",
    "sequence": 1,
    "sent_at": "2026-07-25T00:00:00+00:00",
    "payload": {"connected": True, "torque_enabled": False, "joints": []},
}
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(json.dumps(message).encode(), (host, int(port)))
time.sleep(10)
"""
    staged = store.stage({
        "name": "Stale telemetry",
        "deployment_id": "stale-telemetry",
        "script": script,
        "manifest": {
            "schema_version": 1,
            "telemetry_required": True,
        },
    })
    store.start(staged["id"])
    try:
        for _ in range(100):
            result = store.get(staged["id"])
            if result["state"] != "running":
                break
            time.sleep(0.02)
        assert result["state"] == "failed"
        assert "telemetry became stale" in result["error"]
    finally:
        store.stop_all()


def test_non_robot_deployment_does_not_require_telemetry(tmp_path: Path):
    store = DeploymentStore(
        tmp_path / "deployments",
        telemetry_startup_grace_seconds=0.1,
        telemetry_watchdog_interval_seconds=0.02,
    )
    staged = store.stage({
        "name": "Compute only",
        "deployment_id": "compute-only",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {"schema_version": 1},
    })
    store.start(staged["id"])
    try:
        time.sleep(0.2)
        result = store.get(staged["id"])
        assert result["state"] == "running"
        assert result["telemetry_required"] is False
    finally:
        store.stop_all()


def test_deployment_ownership_is_persisted_preserved_and_not_reassigned(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    owned = store.stage({
        "name": "Leader",
        "deployment_id": "leader",
        "script": "print('owned')\n",
        "manifest": {
            "schema_version": 1,
            "project_id": "leader-follower-demo",
            "workflow_slug": "leader-deploy",
        },
    })
    assert owned["project_id"] == "leader-follower-demo"
    assert owned["workflow_slug"] == "leader-deploy"

    preserved = store.stage({
        "name": "Leader",
        "deployment_id": "leader",
        "script": "print('next revision')\n",
        "manifest": {"schema_version": 1},
    })
    assert preserved["project_id"] == "leader-follower-demo"
    assert preserved["workflow_slug"] == "leader-deploy"
    revision_manifest = json.loads(
        (
            tmp_path
            / "deployments"
            / "leader"
            / "revisions"
            / preserved["staged_revision"]
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert revision_manifest["project_id"] == "leader-follower-demo"
    assert revision_manifest["workflow_slug"] == "leader-deploy"

    with pytest.raises(DeploymentError, match="belongs to project"):
        store.stage({
            "name": "Leader",
            "deployment_id": "leader",
            "script": "print('wrong project')\n",
            "manifest": {
                "schema_version": 1,
                "project_id": "another-project",
                "workflow_slug": "leader-deploy",
            },
        })
    with pytest.raises(DeploymentError, match="belongs to workflow"):
        store.stage({
            "name": "Leader",
            "deployment_id": "leader",
            "script": "print('wrong workflow')\n",
            "manifest": {
                "schema_version": 1,
                "project_id": "leader-follower-demo",
                "workflow_slug": "follower-deploy",
            },
        })


def test_deployment_ownership_requires_a_valid_pair_and_legacy_is_unassigned(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    legacy = store.stage({
        "name": "Legacy",
        "script": "print('legacy')\n",
        "manifest": {"schema_version": 1},
    })
    assert legacy["project_id"] == ""
    assert legacy["workflow_slug"] == ""

    with pytest.raises(DeploymentError, match="requires both"):
        store.stage({
            "name": "Partial",
            "script": "print('partial')\n",
            "manifest": {"project_id": "demo"},
        })
    with pytest.raises(DeploymentError, match="project_id is invalid"):
        store.stage({
            "name": "Invalid",
            "script": "print('invalid')\n",
            "manifest": {
                "project_id": "../demo",
                "workflow_slug": "leader",
            },
        })


def test_stage_rejects_invalid_or_oversized_python(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    with pytest.raises(DeploymentError, match="does not compile"):
        store.stage({"name": "Bad", "script": "if:\n"})
    with pytest.raises(DeploymentError, match="2 MB"):
        store.stage({"name": "Large", "script": "#" * (2 * 1024 * 1024 + 1)})


def test_http_service_requires_auth_and_serves_manifest_and_deployments(tmp_path: Path):
    token = "runtime-pairing-token-" + "x" * 32
    config, _ = _config(tmp_path, token)
    store = DeploymentStore(tmp_path / "deployments")
    server = create_server(config, store, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, health = _request(f"{base}/health")
        assert status == 200
        assert health["auth_required"] is True
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            _request(f"{base}/manifest")
        assert unauthorized.value.code == 401

        _, manifest = _request(f"{base}/manifest", token=token)
        assert manifest["device_id"] == "pi-test"
        status, staged = _request(
            f"{base}/deployments",
            token=token,
            payload={"name": "HTTP", "script": "print('http deployment')\n"},
        )
        assert status == 201
        _, deployments = _request(f"{base}/deployments", token=token)
        assert [item["id"] for item in deployments["deployments"]] == [staged["id"]]
        _, telemetry = _request(
            f"{base}/deployments/{staged['id']}/telemetry",
            token=token,
        )
        assert telemetry["available"] is False
        assert telemetry["stream"] == "robot-state"
    finally:
        server.shutdown()
        server.server_close()
        store.stop_all()


def test_http_package_sync_is_authenticated_and_delegated(tmp_path: Path):
    class FakePackageManager:
        def __init__(self):
            self.payloads = []

        def sync(self, payload):
            self.payloads.append(payload)
            return {
                "ok": True,
                "installed": [{"name": "blacknode-perception", "version": "0.3.0"}],
                "already_present": [],
                "messages": [],
            }

    token = "runtime-pairing-token-" + "x" * 32
    config, _ = _config(tmp_path, token)
    store = DeploymentStore(tmp_path / "deployments")
    package_manager = FakePackageManager()
    server = create_server(
        config,
        store,
        token,
        package_manager=package_manager,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    payload = {
        "packages": [{
            "name": "blacknode-perception",
            "git_url": "https://github.com/temiroff/blacknode-perception.git",
        }],
    }
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            _request(f"{base}/packages/sync", payload=payload)
        assert unauthorized.value.code == 401
        status, result = _request(
            f"{base}/packages/sync",
            token=token,
            payload=payload,
        )
        assert status == 200
        assert result["installed"][0]["name"] == "blacknode-perception"
        assert package_manager.payloads == [payload]
    finally:
        server.shutdown()
        server.server_close()
        store.stop_all()


@pytest.mark.skipif(os.name == "nt", reason="systemd unit paths are POSIX-only")
def test_systemd_unit_uses_absolute_paths_and_process_group_shutdown(tmp_path: Path):
    repo = tmp_path / "runtime"
    config = repo / ".blacknode-runtime" / "runtime.json"
    state = repo / ".blacknode-runtime" / "state"
    repo.mkdir()
    unit = render_unit(
        repo=repo,
        user="alex",
        host="0.0.0.0",
        port=8766,
        config=config,
        state_dir=state,
    )
    assert f"WorkingDirectory={repo}" in unit
    assert "KillMode=control-group" in unit
    assert "--port 8766" in unit
    assert f'ReadWritePaths="{state}"' in unit
    assert f'Environment="BLACKNODE_PACKAGE_PATH={repo / "packages"}"' in unit
    assert f'ExecStart="{repo / "scripts" / "with_ros_env.sh"}"' in unit
    assert "Wants=network-online.target" in unit
    assert "After=network-online.target" in unit
    assert "blacknode-hardware.service" not in unit
