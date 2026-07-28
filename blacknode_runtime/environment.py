"""Runtime process environments with installed package ROS 2 workspaces."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

from .package_workspaces import (
    PackageWorkspaceError,
    declared_ros2_workspaces,
    workspace_setup_path,
)


class RuntimeEnvironmentError(RuntimeError):
    pass


def package_workspace_setups(
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    values = environment if environment is not None else os.environ
    package_path = str(values.get("BLACKNODE_PACKAGE_PATH") or "")
    setups: set[Path] = set()
    for root_value in package_path.split(os.pathsep):
        if not root_value.strip():
            continue
        root = Path(root_value).expanduser().resolve()
        if not root.is_dir():
            continue
        for manifest in root.glob("*/blacknode-package.toml"):
            try:
                workspaces = declared_ros2_workspaces(manifest.parent)
            except PackageWorkspaceError:
                # A malformed optional package must not prevent unrelated
                # deployments or managed services from starting.
                continue
            for workspace in workspaces:
                candidate = workspace_setup_path(workspace)
                if candidate.is_file():
                    setups.add(candidate.resolve())
        # Preserve discovery for packages released before ros2-workspaces was
        # part of the manifest contract.
        for candidate in root.glob(
            "*/components/*/adapters/*/ros2_ws/install/setup.bash"
        ):
            if candidate.is_file():
                setups.add(candidate.resolve())
        for candidate in root.glob("*/ros2_ws/install/setup.bash"):
            if candidate.is_file():
                setups.add(candidate.resolve())
    return sorted(setups, key=lambda value: str(value))


def runtime_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment containing newly built package workspaces.

    Package synchronization can happen after the Runtime service itself starts.
    Sourcing at child-process launch makes those workspaces immediately
    available to deployments and managed attachment services.
    """

    base = dict(environment if environment is not None else os.environ)
    setups = package_workspace_setups(base)
    if not setups:
        return base
    script = (
        'set -e; for setup in "$@"; do set +u; source "$setup"; '
        "set -u; done; env -0"
    )
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "blacknode-package-ros-environment",
                *(str(path) for path in setups),
            ],
            env=base,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeEnvironmentError(
            f"could not load installed package ROS 2 workspaces: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeEnvironmentError(
            "could not load installed package ROS 2 workspaces"
            + (f": {detail}" if detail else "")
        )
    resolved: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        resolved[key.decode("utf-8", errors="surrogateescape")] = value.decode(
            "utf-8",
            errors="surrogateescape",
        )
    return resolved or base
