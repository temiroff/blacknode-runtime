from __future__ import annotations

import json
import os
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from blacknode_runtime.auth import load_auth_token
from blacknode_runtime.config import RuntimeConfig
from blacknode_runtime.deployments import DeploymentError, DeploymentStore
from blacknode_runtime.manifest import FEATURES, runtime_manifest
from blacknode_runtime.server import create_server
from scripts.render_systemd_unit import render_unit


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
    assert load_auth_token(token_path) == "x" * 48
    assert "x" * 48 not in config_path.read_text(encoding="utf-8")


def test_manifest_reports_real_runtime_features(tmp_path: Path):
    config, _ = _config(tmp_path)
    manifest = runtime_manifest(config)
    assert manifest["service"] == "blacknode-runtime"
    assert manifest["protocol_version"] == 1
    assert set(FEATURES) <= set(manifest["features"])
    assert manifest["python"]["version"]
    assert manifest["device_id"] == "pi-test"


def test_blacknode_package_manifest_loads():
    path = Path(__file__).resolve().parents[1] / "blacknode-package.toml"
    package = tomllib.loads(path.read_text(encoding="utf-8"))["package"]
    assert package["name"] == "blacknode-runtime"
    assert package["layer"] == "runtime"
    assert package["component-mode"] is True


def test_stage_start_logs_and_revision_rollback(tmp_path: Path):
    store = DeploymentStore(tmp_path / "deployments")
    first = store.stage({
        "name": "Example",
        "deployment_id": "example",
        "script": "print('revision one')\n",
        "manifest": {"schema_version": 1},
    })
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
    assert len(second["revisions"]) == 2
    second = store.start("example")
    for _ in range(50):
        second = store.get("example")
        if second["state"] != "running":
            break
        time.sleep(0.02)
    rolled_back = store.rollback("example")
    assert rolled_back["staged_revision"] == first["staged_revision"]


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
