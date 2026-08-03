from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from blacknode_runtime import deployments as deployment_module
from blacknode_runtime.auth import load_auth_token, token_fingerprint
from blacknode_runtime.config import RuntimeConfig
from blacknode_runtime.deployments import DeploymentError, DeploymentStore
from blacknode_runtime.diagnostics import publish_ros2_armed_control, ros2_diagnostics
from blacknode_runtime import environment as environment_module
from blacknode_runtime.manifest import FEATURES, runtime_manifest
from blacknode_runtime.managed_services import ManagedServiceError, ManagedServiceStore
from blacknode_runtime.package_manager import PackageManager, PackageSyncError
from blacknode_runtime.ros2_streams import Ros2TopicStreamError, Ros2TopicStreamStore
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


def test_runtime_environment_loads_new_package_ros_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_dir = tmp_path / "blacknode-perception"
    setup = (
        package_dir
        / "providers"
        / "camera_ws"
        / "install"
        / "setup.bash"
    )
    setup.parent.mkdir(parents=True)
    setup.write_text("# test workspace\n", encoding="utf-8")
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-perception"\n'
        'version = "0.3.3"\n'
        "\n"
        "[components.camera.adapters.ros2]\n"
        'ros2-workspaces = ["providers/camera_ws"]\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["environment"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=b"AMENT_PREFIX_PATH=/camera/install\0BASE=value\0",
            stderr=b"",
        )

    monkeypatch.setattr(environment_module.subprocess, "run", fake_run)
    result = environment_module.runtime_environment({
        "BLACKNODE_PACKAGE_PATH": str(tmp_path),
        "BASE": "original",
    })

    assert str(setup.resolve()) in seen["command"]
    assert seen["environment"]["BASE"] == "original"
    assert result["AMENT_PREFIX_PATH"] == "/camera/install"
    assert result["BASE"] == "value"


def test_package_sync_repairs_missing_declared_ros2_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import blacknode.packages as packages

    manager = PackageManager(tmp_path / "packages")
    package_dir = manager.root / "blacknode-perception"
    package_dir.mkdir()
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-perception"\n'
        'version = "0.3.3"\n'
        'description = "Perception"\n'
        'requires-blacknode = ">=0.3.0"\n'
        "\n"
        "[components.camera]\n"
        "default = true\n"
        "\n"
        "[components.camera.adapters.ros2]\n"
        "default = true\n"
        'ros2-workspaces = ["providers/camera_ws"]\n',
        encoding="utf-8",
    )
    info = SimpleNamespace(
        name="blacknode-perception",
        version="0.3.3",
        node_types=["CameraROS2Provider"],
        ok=True,
        error="",
        enabled_components=["camera"],
        enabled_adapters=["camera/ros2"],
    )
    monkeypatch.setattr(packages, "load_package", lambda _path: info)
    monkeypatch.setattr(
        packages,
        "ensure_component_enabled",
        lambda *_args, **_kwargs: info,
    )
    monkeypatch.setattr(
        packages,
        "ensure_adapter_enabled",
        lambda *_args, **_kwargs: info,
    )
    setup_calls = []

    def repair(package_path, *, progress):
        setup_calls.append(Path(package_path))
        setup = package_dir / "providers/camera_ws/install/setup.bash"
        setup.parent.mkdir(parents=True)
        setup.write_text("# built\n", encoding="utf-8")
        progress("Built declared ROS 2 workspace")
        return []

    monkeypatch.setattr(packages, "install_prerequisites", repair)

    result = manager.sync({
        "packages": [{
            "name": "blacknode-perception",
            "git_url": "https://github.com/temiroff/blacknode-perception.git",
            "version": "0.3.3",
        }],
    })

    assert result["ok"] is True
    assert setup_calls == [package_dir]
    assert any(
        "Repairing blacknode-perception ROS 2 workspace setup" in message
        for message in result["messages"]
    )


def test_package_sync_rejects_unbuilt_declared_ros2_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import blacknode.packages as packages

    manager = PackageManager(tmp_path / "packages")
    package_dir = manager.root / "blacknode-example"
    package_dir.mkdir()
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-example"\n'
        'version = "1.0.0"\n'
        "\n"
        "[components.sensor.adapters.ros2]\n"
        'ros2-workspaces = ["robot_ws"]\n',
        encoding="utf-8",
    )
    info = SimpleNamespace(
        name="blacknode-example",
        version="1.0.0",
        node_types=[],
        ok=True,
        error="",
    )
    monkeypatch.setattr(packages, "load_package", lambda _path: info)
    monkeypatch.setattr(
        packages,
        "ensure_component_enabled",
        lambda *_args, **_kwargs: info,
    )
    monkeypatch.setattr(
        packages,
        "ensure_adapter_enabled",
        lambda *_args, **_kwargs: info,
    )
    monkeypatch.setattr(
        packages,
        "install_prerequisites",
        lambda *_args, **_kwargs: [
            "package setup script failed"
        ],
    )

    with pytest.raises(
        PackageSyncError,
        match="did not build its declared ROS 2 workspace overlays",
    ):
        manager.sync({
            "packages": [{
                "name": "blacknode-example",
                "git_url": "https://example.com/blacknode-example.git",
                "components": ["sensor"],
                "adapters": [{
                    "component": "sensor",
                    "adapter": "ros2",
                }],
            }],
        })


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
    manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    package = manifest["package"]
    assert package["name"] == "blacknode-runtime"
    assert package["layer"] == "runtime"
    assert package["component-mode"] is True
    assert set(manifest["components"]) == {"deployment"}
    capabilities = set(manifest["components"]["deployment"]["capabilities"])
    assert "deployment.rollout" in capabilities
    assert "deployment.rollback" in capabilities
    assert not any(capability.startswith("runtime.workflow") for capability in capabilities)


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
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-skills"\n'
        'version = "0.1.0"\n',
        encoding="utf-8",
    )
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
            "components": ["follow"],
            "adapters": [{
                "component": "follow",
                "adapter": "ros2",
            }],
        }],
    })

    assert activated == [
        ("component", "blacknode-skills", "follow"),
        ("adapter", "blacknode-skills", "follow", "ros2"),
    ]
    assert result["activated"] == [
        {
            "package": "blacknode-skills",
            "component": "follow",
            "adapter": "",
        },
        {
            "package": "blacknode-skills",
            "component": "follow",
            "adapter": "ros2",
        },
    ]


def test_package_sync_update_refreshes_existing_package_at_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import blacknode.packages as packages

    manager = PackageManager(tmp_path / "packages")
    package_dir = manager.root / "blacknode-skills"
    package_dir.mkdir()
    (package_dir / "blacknode-package.toml").write_text(
        "[package]\n"
        'name = "blacknode-skills"\n'
        'version = "0.2.3"\n'
        'description = "Skills"\n'
        'requires-blacknode = ">=0.3.0"\n',
        encoding="utf-8",
    )
    info = SimpleNamespace(
        name="blacknode-skills",
        version="0.2.3",
        node_types=["ROS2LeaderFollower"],
        ok=True,
        error="",
    )
    monkeypatch.setattr(packages, "load_package", lambda _path: info)
    monkeypatch.setattr(
        packages,
        "install_prerequisites",
        lambda _path, **_kwargs: None,
    )
    refreshed = []
    monkeypatch.setattr(
        manager,
        "_update_existing",
        lambda name, path, target, messages: refreshed.append(
            (name, path, target)
        ),
    )

    result = manager.sync({
        "packages": [{
            "name": "blacknode-skills",
            "git_url": "https://github.com/temiroff/blacknode-skills.git",
            "update": True,
        }],
    })

    assert refreshed == [
        ("blacknode-skills", package_dir, "latest revision"),
    ]
    assert result["already_present"][0]["version"] == "0.2.3"


def test_package_update_preserves_local_changes_in_git_stash(tmp_path: Path):
    origin = tmp_path / "origin.git"
    package_dir = tmp_path / "blacknode-perception"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(package_dir)],
        check=True,
        capture_output=True,
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(package_dir), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("config", "user.email", "runtime-test@example.com")
    git("config", "user.name", "Runtime Test")
    git("config", "core.autocrlf", "false")
    tracked = package_dir / "provider.txt"
    tracked.write_text("published provider\n", encoding="utf-8")
    git("add", "provider.txt")
    git("commit", "-m", "Initial provider")
    git("push", "--set-upstream", "origin", "HEAD")

    tracked.write_text("device-local provider edit\n", encoding="utf-8")
    untracked = package_dir / "device-notes.txt"
    untracked.write_text("local calibration notes\n", encoding="utf-8")
    messages: list[str] = []

    PackageManager._update_existing(
        "blacknode-perception",
        package_dir,
        "0.3.3",
        messages,
    )

    assert git("status", "--porcelain").stdout == ""
    assert tracked.read_text(encoding="utf-8") == "published provider\n"
    stash_list = git("stash", "list").stdout
    assert "blacknode-runtime package sync before 0.3.3" in stash_list
    stash_files = git(
        "stash",
        "show",
        "--include-untracked",
        "--name-only",
        "stash@{0}",
    ).stdout
    assert "provider.txt" in stash_files
    assert "device-notes.txt" in stash_files
    assert any(
        "recover them with git -C" in message
        for message in messages
    )


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


def test_deployment_workflow_snapshot_and_legacy_export_recovery(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    workflow = {
        "kind": "blacknode.workflow",
        "schema_version": 1,
        "name": "Recoverable",
        "node_meta": {},
        "edges": [],
    }
    staged = store.stage({
        "name": "Snapshot",
        "deployment_id": "snapshot",
        "script": "print('snapshot')\n",
        "workflow": workflow,
    })
    captured = store.workflow(staged["id"])
    assert captured["source"] == "snapshot"
    assert captured["workflow"] == workflow

    legacy = store.stage({
        "name": "Legacy export",
        "deployment_id": "legacy-export",
        "script": f"_WORKFLOW = {workflow!r}\nprint('legacy')\n",
    })
    recovered = store.workflow(legacy["id"])
    assert recovered["source"] == "generated_script"
    assert recovered["workflow"] == workflow

    missing = store.stage({
        "name": "No graph",
        "deployment_id": "no-graph",
        "script": "print('no graph')\n",
    })
    with pytest.raises(DeploymentError, match="stage it again"):
        store.workflow(missing["id"])


def test_ros2_diagnostics_reports_robot_endpoints_and_duplicate_nodes():
    def runner(args: list[str], _timeout: float):
        command = " ".join(args)
        if command == "node list":
            stdout = "/driver\n/driver\n/controller\n"
        elif command == "topic list -t":
            stdout = (
                "/leader/joint_states [sensor_msgs/msg/JointState]\n"
                "/follower/robot_control [std_msgs/msg/String]\n"
                "/rosout [rcl_interfaces/msg/Log]\n"
            )
        elif command == "service list":
            stdout = "/driver/get_parameters\n"
        else:
            stdout = "Publisher count: 1\nSubscription count: 1\n"
        return {
            "command": ["ros2", *args],
            "ok": True,
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "",
            "error": "",
        }

    result = ros2_diagnostics(runner)

    assert result["ok"] is True
    assert result["available"] is True
    assert [detail["topic"] for detail in result["topic_details"]] == [
        "/follower/robot_control",
        "/leader/joint_states",
        "/rosout",
    ]
    assert result["warnings"] == ["Duplicate ROS 2 node names: /driver"]


def test_ros2_diagnostics_filters_destroyed_endpointless_helper_nodes():
    def runner(args: list[str], _timeout: float):
        command = " ".join(args)
        if command == "node list":
            stdout = "/active_driver\n/blacknode_native_read_old\n"
        elif command == "topic list -t":
            stdout = "/robot/joint_states [sensor_msgs/msg/JointState]\n"
        elif command == "service list":
            stdout = "/blacknode_native_read_old/get_type_description\n"
        else:
            stdout = (
                "Publisher count: 1\n\n"
                "Node name: active_driver\n"
                "Node namespace: /\n"
                "Endpoint type: PUBLISHER\n\n"
                "Subscription count: 0\n"
            )
        return {
            "command": ["ros2", *args],
            "ok": True,
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "",
            "error": "",
        }

    result = ros2_diagnostics(runner)

    assert result["nodes"] == ["/active_driver"]
    assert result["stale_nodes"] == ["/blacknode_native_read_old"]


def test_arm_control_rejects_topics_outside_the_deployment_namespace():
    result = publish_ros2_armed_control(
        "/blacknode/leader_follower/follower/control;unsafe",
        True,
    )

    assert result["ok"] is False
    assert result["error"] == "deployment arm control topic is invalid"


def test_running_deployment_has_explicit_arm_and_disarm_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    published = []
    monkeypatch.setattr(
        deployment_module,
        "publish_ros2_armed_control",
        lambda topic, armed: (
            published.append((topic, armed))
            or {"ok": True, "topic": topic, "armed": armed}
        ),
    )
    store = DeploymentStore(tmp_path / "deployments")
    staged = store.stage({
        "name": "Follower",
        "deployment_id": "follower",
        "script": "import time\ntime.sleep(10)\n",
        "manifest": {
            "schema_version": 1,
            "motion_controls": [{
                "kind": "ros2_leader_follower",
                "node_id": "follow",
                "topic": "/blacknode/leader_follower/follower/control",
            }],
        },
    })
    assert staged["motion_armed"] is False
    store.start(staged["id"])
    try:
        armed = store.set_motion_armed(staged["id"], True)
        assert armed["armed"] is True
        assert armed["deployment"]["motion_armed"] is True
        assert published == [
            ("/blacknode/leader_follower/follower/control", True),
        ]
    finally:
        stopped = store.stop(staged["id"])
    assert stopped["motion_armed"] is False


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
        state = {
            "kind": "blacknode.device-state",
            "schema_version": 1,
            "device_id": "leader-arm",
            "connected": True,
            "armed": True,
            "torque_enabled": True,
            "joint_state": {
                "kind": "blacknode.joint-state",
                "schema_version": 1,
                "position_unit": "radian",
                "velocity_unit": "radian/s",
                "positions": {"shoulder": 0.2, "elbow": -0.1},
                "velocities": {"shoulder": 0.0, "elbow": 0.0},
                "efforts": {},
                "limits": {
                    "shoulder": {"lower": -1.5, "upper": 1.5},
                    "elbow": {"lower": -0.75, "upper": 0.75},
                },
            },
            "faults": [],
        }
        assert publisher.publish_device_state(state)
        for _ in range(20):
            sample = receiver.latest()
            if sample["available"]:
                break
            time.sleep(0.01)
        assert sample["available"] is True
        assert sample["stale"] is False
        assert sample["payload"] == state
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
        "kind": "blacknode.device-state",
        "schema_version": 1,
        "device_id": "gripper",
        "connected": True,
        "armed": False,
        "torque_enabled": False,
        "joint_state": {
            "kind": "blacknode.joint-state",
            "schema_version": 1,
            "position_unit": "radian",
            "velocity_unit": "radian/s",
            "positions": {"gripper": 0.12},
            "velocities": {"gripper": 0.0},
            "efforts": {},
            "limits": {},
        },
        "faults": [],
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
        assert sample["payload"]["joint_state"]["positions"]["gripper"] == 0.12
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
    "payload": {
        "kind": "blacknode.device-state",
        "schema_version": 1,
        "device_id": "stale-arm",
        "connected": True,
        "armed": False,
        "torque_enabled": False,
        "joint_state": None,
        "faults": [],
    },
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
            payload={
                "name": "HTTP",
                "script": "print('http deployment')\n",
                "workflow": {
                    "kind": "blacknode.workflow",
                    "schema_version": 1,
                    "name": "HTTP",
                    "node_meta": {},
                    "edges": [],
                },
            },
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
        _, captured = _request(
            f"{base}/deployments/{staged['id']}/workflow",
            token=token,
        )
        assert captured["source"] == "snapshot"
        assert captured["workflow"]["name"] == "HTTP"
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


def test_managed_ros2_service_is_scoped_and_reports_interfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from blacknode_runtime import managed_services as service_module

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.exit_code = None
            self.terminated = False

        def poll(self):
            return self.exit_code

        def terminate(self):
            self.terminated = True
            self.exit_code = 0

        def wait(self, timeout=None):
            return self.exit_code

        def kill(self):
            self.exit_code = -9

    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        process.command = command
        process.environment = kwargs["env"]
        processes.append(process)
        return process

    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        service_module,
        "runtime_environment",
        lambda: {"BLACKNODE_PACKAGE_ROS": "ready"},
    )
    monkeypatch.setattr(
        service_module,
        "inspect_ros2_interfaces",
        lambda interfaces: {
            "ok": True,
            "interfaces": [
                {**item, "ready": True, "publishers": 1}
                for item in interfaces
            ],
            "missing": [],
        },
    )
    store = ManagedServiceStore(tmp_path / "services")
    payload = {
        "name": "Front RGB-D camera",
        "command": {
            "verb": "launch",
            "package": "perception_camera",
            "target": "rgbd_camera.launch.py",
            "arguments": [],
        },
        "interfaces": [{
            "topic": "/camera/rgb/image_raw",
            "type": "sensor_msgs/msg/Image",
            "direction": "publisher",
        }],
    }

    started = store.start("front-camera", payload)
    assert started["state"] == "running"
    assert started["command"] == [
        "ros2",
        "launch",
        "perception_camera",
        "rgbd_camera.launch.py",
    ]
    assert started["diagnostics"]["ok"] is True
    assert len(processes) == 1
    assert processes[0].environment["BLACKNODE_PACKAGE_ROS"] == "ready"

    # Repeating the same request is idempotent and does not duplicate a camera.
    assert store.start("front-camera", payload)["pid"] == 4321
    assert len(processes) == 1

    stopped = store.stop("front-camera")
    assert stopped["state"] == "stopped"
    assert processes[0].terminated is True


def test_managed_ros2_service_rejects_shell_commands(tmp_path: Path):
    store = ManagedServiceStore(tmp_path / "services")
    with pytest.raises(ManagedServiceError, match="verb must be"):
        store.start("front-camera", {
            "command": {
                "verb": "exec",
                "package": "sh",
                "target": "-c",
                "arguments": ["rm -rf /"],
            },
        })


def test_http_managed_service_lifecycle_is_authenticated(tmp_path: Path):
    class FakeServiceStore:
        def __init__(self):
            self.started = []
            self.stopped = []

        def list(self):
            return []

        def get(self, service_id):
            return {"id": service_id, "state": "running"}

        def start(self, service_id, payload):
            self.started.append((service_id, payload))
            return {"id": service_id, "state": "running"}

        def stop(self, service_id):
            self.stopped.append(service_id)
            return {"id": service_id, "state": "stopped"}

        def logs(self, service_id, limit):
            return f"{service_id}:{limit}"

        def stop_all(self):
            return None

    token = "runtime-pairing-token-" + "x" * 32
    config, _ = _config(tmp_path, token)
    store = DeploymentStore(tmp_path / "deployments")
    service_store = FakeServiceStore()
    server = create_server(
        config,
        store,
        token,
        service_store=service_store,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            _request(f"{base}/services")
        assert unauthorized.value.code == 401
        _, services = _request(f"{base}/services", token=token)
        assert services == {"services": []}
        _, started = _request(
            f"{base}/services/front-camera/start",
            token=token,
            payload={"command": {"verb": "run"}},
        )
        assert started["state"] == "running"
        _, stopped = _request(
            f"{base}/services/front-camera/stop",
            token=token,
            payload={},
        )
        assert stopped["state"] == "stopped"
        assert service_store.started[0][0] == "front-camera"
        assert service_store.stopped == ["front-camera"]
    finally:
        server.shutdown()
        server.server_close()
        store.stop_all()


class _FakeRos2TopicAdapter:
    def __init__(self):
        self.running = {}
        self.stopped = []

    def discover_type(self, topic):
        assert topic == "/scan"
        return "sensor_msgs/msg/LaserScan"

    def start(self, config):
        self.running[config["topic"]] = dict(config)
        return {
            "running": True,
            "backend": "native",
            "topic": config["topic"],
            "message_type": config["message_type"],
            "messages": [{"ranges": [1.0, 2.0]}],
            "latest": {"ranges": [1.0, 2.0]},
            "received": 1,
            "source_fresh": True,
            "last_message_time_ns": 42,
            "age_seconds": 0.01,
            "stale_after_seconds": config["stale_after_seconds"],
        }

    def once(self, config):
        return self.start(config) | {"running": False}

    def status(self, topic):
        return self.start(self.running[topic])

    def stop(self, topic):
        config = self.running.pop(topic)
        self.stopped.append(topic)
        return {
            "running": False,
            "backend": "native",
            "topic": topic,
            "message_type": config["message_type"],
            "messages": [],
            "received": 0,
            "source_fresh": False,
        }

    def outputs(self, status, report):
        return {
            "running": bool(status.get("running")),
            "message": status.get("latest") or {},
            "messages": list(status.get("messages") or []),
            "stream": {
                "kind": "blacknode.message-stream",
                "stream_id": "topic-subscriber:" + str(status.get("topic") or ""),
                "topic": status.get("topic") or "",
            },
            "status": {
                "kind": "blacknode.stream-status",
                "state": "ready" if status.get("source_fresh") else "stopped",
                "source_fresh": bool(status.get("source_fresh")),
            },
            "received": int(status.get("received") or 0),
            "backend": str(status.get("backend") or "native"),
            "report": report,
        }


def test_remote_ros2_topic_store_discovers_streams_and_stops_idempotently():
    adapter = _FakeRos2TopicAdapter()
    store = Ros2TopicStreamStore(adapter)

    started = store.start("editor-scan", {"topic": "/scan"})
    assert started["outputs"]["running"] is True
    assert started["outputs"]["message"]["ranges"] == [1.0, 2.0]
    assert adapter.running["/scan"]["message_type"] == "sensor_msgs/msg/LaserScan"

    status = store.status("editor-scan")
    assert status["outputs"]["received"] == 1
    assert status["outputs"]["status"]["source_fresh"] is True

    stopped = store.stop("editor-scan")
    assert stopped["outputs"]["running"] is False
    assert store.stop("editor-scan")["outputs"]["running"] is False
    assert adapter.stopped == ["/scan"]


def test_remote_ros2_topic_store_rejects_unscoped_values():
    store = Ros2TopicStreamStore(_FakeRos2TopicAdapter())
    with pytest.raises(Ros2TopicStreamError, match="topic is invalid"):
        store.start("editor-scan", {"topic": "scan"})
    with pytest.raises(Ros2TopicStreamError, match="id is invalid"):
        store.start("../scan", {"topic": "/scan"})


def test_http_remote_ros2_topic_lifecycle_is_authenticated(tmp_path: Path):
    token = "runtime-pairing-token-" + "x" * 32
    config, _ = _config(tmp_path, token)
    deployment_store = DeploymentStore(tmp_path / "deployments")
    topic_store = Ros2TopicStreamStore(_FakeRos2TopicAdapter())
    server = create_server(
        config,
        deployment_store,
        token,
        ros2_topic_store=topic_store,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            _request(f"{base}/ros2/topics")
        assert unauthorized.value.code == 401

        _, started = _request(
            f"{base}/ros2/topics/editor-scan/start",
            token=token,
            payload={"topic": "/scan", "message_type": ""},
        )
        assert started["outputs"]["running"] is True
        _, status = _request(
            f"{base}/ros2/topics/editor-scan",
            token=token,
        )
        assert status["outputs"]["message"]["ranges"] == [1.0, 2.0]
        _, stopped = _request(
            f"{base}/ros2/topics/editor-scan/stop",
            token=token,
            payload={},
        )
        assert stopped["outputs"]["running"] is False
    finally:
        server.shutdown()
        server.server_close()
        topic_store.stop_all()
        deployment_store.stop_all()


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
