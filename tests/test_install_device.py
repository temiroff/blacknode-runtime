from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from blacknode_runtime import __version__


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "install-device.sh"


def test_runtime_release_versions_match():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    package = tomllib.loads(
        (REPO / "blacknode-package.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["version"] == __version__
    assert package["package"]["version"] == __version__


def test_runtime_service_scripts_support_isolated_instances():
    install_service = (REPO / "install-service.sh").read_text(encoding="utf-8")
    service = (REPO / "service.sh").read_text(encoding="utf-8")

    for script in (install_service, service):
        assert 'instance="${BLACKNODE_RUNTIME_INSTANCE:-}"' in script
        assert 'unit_name="blacknode-runtime${instance:+-$instance}.service"' in script
    assert 'comment "Blacknode runtime${instance:+ $instance}"' in install_service


def test_package_install_restarts_existing_unit_without_rewriting_its_port():
    install_package = (REPO / "install-package.sh").read_text(encoding="utf-8")

    assert "sudo systemctl restart blacknode-runtime.service" in install_package
    assert '"$repo_dir/install-service.sh"' not in install_package


@pytest.mark.skipif(os.name == "nt", reason="device installer targets Linux")
def test_device_installer_has_valid_bash_and_help():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "complete Blacknode device stack" in result.stdout
    assert "--plan" in result.stdout
    assert "--stop-deployments" in result.stdout
