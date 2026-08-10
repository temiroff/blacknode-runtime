"""Truthful runtime and package inventory for deployment preflight."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from .config import RuntimeConfig


FEATURES = [
    "manifest_v1",
    "deployment_bundle_v1",
    "process_supervision_v1",
    "deployment_logs_v1",
    "rollback_v1",
    "package_sync_v1",
    "package_refresh_v1",
    "component_sync_v1",
    "declared_ros2_workspaces_v1",
    "deployment_ownership_v1",
    "deployment_telemetry_v1",
    "required_telemetry_watchdog_v1",
    "single_target_deployment_v1",
    "deployment_workflow_v1",
    "deployment_motion_control_v1",
    "deployment_mapping_control_v1",
    "ros2_diagnostics_v1",
    "managed_ros2_services_v1",
    "remote_ros2_topic_stream_v1",
    "remote_ros2_image_stream_v1",
]


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _workspace_package_dirs(root: Path | None) -> list[Path]:
    package_dirs: list[Path] = []
    if root is not None:
        package_dirs.append(root / "packages")
    for raw_path in os.environ.get("BLACKNODE_PACKAGE_PATH", "").split(os.pathsep):
        if raw_path.strip():
            package_dirs.append(Path(raw_path).expanduser())
    return list(dict.fromkeys(path.resolve() for path in package_dirs))


def _workspace_packages(root: Path | None) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for packages_dir in _workspace_package_dirs(root):
        if not packages_dir.is_dir():
            continue
        for manifest_path in sorted(packages_dir.glob("*/blacknode-package.toml")):
            try:
                payload = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                package = payload.get("package", {})
                name = str(package.get("name") or "").strip()
                version = str(package.get("version") or "").strip()
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if name:
                found[name] = {
                    "name": name,
                    "version": version,
                    "source": "workspace",
                }
    return sorted(found.values(), key=lambda item: item["name"])


def _installed_blacknode_packages() -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        normalized = name.lower().replace("_", "-")
        if normalized == "blacknode" or normalized.startswith("blacknode-"):
            found[normalized] = {
                "name": normalized,
                "version": str(distribution.version or ""),
                "source": "python",
            }
    return sorted(found.values(), key=lambda item: item["name"])


def runtime_manifest(config: RuntimeConfig) -> dict[str, Any]:
    root = Path(config.blacknode_root) if config.blacknode_root else None
    blacknode_version = _distribution_version("blacknode")
    packages_by_name = {
        item["name"]: item
        for item in _installed_blacknode_packages()
    }
    packages_by_name.update({
        item["name"]: item
        for item in _workspace_packages(root)
    })
    packages_by_name["blacknode-runtime"] = {
        "name": "blacknode-runtime",
        "version": __version__,
        "source": "runtime",
    }
    packages = sorted(packages_by_name.values(), key=lambda item: item["name"])
    try:
        from blacknode.node import _NODE_REGISTRY
        node_types = sorted(_NODE_REGISTRY)
    except Exception:
        node_types = []
    return {
        "service": "blacknode-runtime",
        "protocol_version": 1,
        "runtime_version": __version__,
        "device_id": config.device_id,
        "features": list(FEATURES),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "blacknode": {
            "installed": bool(blacknode_version),
            "version": blacknode_version,
            "root": str(root) if root else "",
        },
        "packages": packages,
        "node_types": node_types,
        "hardware_url": config.hardware_url,
    }
