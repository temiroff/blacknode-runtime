"""ROS 2 workspace declarations owned by synchronized Blacknode packages."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class PackageWorkspaceError(RuntimeError):
    pass


def _component_name(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        "-",
        str(value or "").strip().lower(),
    ).strip("-")


def _named_tables(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _component_name(name): config
        for name, config in value.items()
        if _component_name(name) and isinstance(config, Mapping)
    }


def _workspace_values(table: Mapping[str, Any], label: str) -> list[str]:
    raw = table.get("ros2-workspaces", table.get("ros2_workspaces", []))
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise PackageWorkspaceError(f"{label} ros2-workspaces must be a list")
    values: list[str] = []
    for item in raw:
        value = str(item).strip() if isinstance(item, str) else ""
        if not value:
            raise PackageWorkspaceError(
                f"{label} ros2-workspaces must contain non-empty paths"
            )
        values.append(value)
    return values


def _owned_workspace(package_dir: Path, value: str, label: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        raise PackageWorkspaceError(
            f"{label} ROS 2 workspace must be relative to the package: {value}"
        )
    resolved = (package_dir / raw).resolve()
    if resolved == package_dir or package_dir not in resolved.parents:
        raise PackageWorkspaceError(
            f"{label} ROS 2 workspace escapes the package: {value}"
        )
    return resolved


def declared_ros2_workspaces(
    package_dir: Path,
    *,
    components: Iterable[str] | None = None,
    adapters: Iterable[tuple[str, str]] | None = None,
) -> list[Path]:
    """Return manifest-declared workspace roots for all or selected capabilities.

    Passing neither ``components`` nor ``adapters`` returns every declaration,
    which is used to compose the environment from already-built overlays.
    Passing either selects package-wide declarations plus only the requested
    component and adapter declarations, which keeps optional providers isolated
    during package synchronization.
    """

    package_dir = Path(package_dir).expanduser().resolve()
    manifest_path = package_dir / "blacknode-package.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PackageWorkspaceError(
            f"could not read ROS 2 workspace declarations from {manifest_path}: {exc}"
        ) from exc

    selected = components is not None or adapters is not None
    component_names = {
        _component_name(value)
        for value in (components or [])
        if _component_name(value)
    }
    adapter_names = {
        (_component_name(component), _component_name(adapter))
        for component, adapter in (adapters or [])
        if _component_name(component) and _component_name(adapter)
    }
    component_names.update(component for component, _adapter in adapter_names)

    declarations: list[tuple[str, str]] = []
    package_table = (
        manifest.get("package")
        if isinstance(manifest.get("package"), Mapping)
        else {}
    )
    for table, label in (
        (manifest, "package"),
        (package_table, "package"),
    ):
        declarations.extend(
            (value, label)
            for value in _workspace_values(table, label)
        )

    component_tables = _named_tables(manifest.get("components"))
    for component_name, component in component_tables.items():
        if not selected or component_name in component_names:
            label = f"component {component_name}"
            declarations.extend(
                (value, label)
                for value in _workspace_values(component, label)
            )
        adapter_tables = _named_tables(component.get("adapters"))
        for adapter_name, adapter in adapter_tables.items():
            if selected and (component_name, adapter_name) not in adapter_names:
                continue
            label = f"adapter {component_name}@{adapter_name}"
            declarations.extend(
                (value, label)
                for value in _workspace_values(adapter, label)
            )

    workspaces: list[Path] = []
    for value, label in declarations:
        workspace = _owned_workspace(package_dir, value, label)
        if workspace not in workspaces:
            workspaces.append(workspace)
    return workspaces


def workspace_setup_path(workspace: Path) -> Path:
    return Path(workspace) / "install" / "setup.bash"
