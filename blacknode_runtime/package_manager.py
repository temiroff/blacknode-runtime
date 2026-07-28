"""Idempotent installation of workflow extension packages on a device."""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .package_workspaces import (
    PackageWorkspaceError,
    declared_ros2_workspaces,
    workspace_setup_path,
)


_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMPONENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_PACKAGES_PER_REQUEST = 32


class PackageSyncError(RuntimeError):
    pass


class PackageManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_packages = payload.get("packages")
        if not isinstance(raw_packages, list):
            raise PackageSyncError("packages must be a list")
        if len(raw_packages) > _MAX_PACKAGES_PER_REQUEST:
            raise PackageSyncError(
                f"one request may contain at most {_MAX_PACKAGES_PER_REQUEST} packages"
            )

        specs = [self._package_spec(item) for item in raw_packages]
        names = [item["name"] for item in specs]
        if len(names) != len(set(names)):
            raise PackageSyncError("package names must be unique")

        installed: list[dict[str, Any]] = []
        present: list[dict[str, Any]] = []
        activated: list[dict[str, str]] = []
        messages: list[str] = []
        with self._lock:
            for spec in specs:
                try:
                    package_dir = self.root / spec["name"]
                    if package_dir.exists():
                        info = self._load_existing(spec, package_dir, messages)
                        present.append(self._summary(info))
                        continue
                    info = self._install(spec, messages)
                    installed.append(self._summary(info))
                except PackageSyncError:
                    raise
                except Exception as exc:
                    raise PackageSyncError(
                        f"{spec['name']} synchronization failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
            for spec in specs:
                try:
                    activated.extend(self._activate_requirements(spec, messages))
                except PackageSyncError:
                    raise
                except Exception as exc:
                    raise PackageSyncError(
                        f"{spec['name']} component activation failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
            for spec in specs:
                try:
                    self._ensure_ros2_workspaces(spec, messages)
                except PackageSyncError:
                    raise
                except Exception as exc:
                    raise PackageSyncError(
                        f"{spec['name']} ROS 2 workspace setup failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

        return {
            "ok": True,
            "installed": installed,
            "already_present": present,
            "activated": activated,
            "messages": messages[-100:],
        }

    def _ensure_ros2_workspaces(
        self,
        spec: dict[str, Any],
        messages: list[str],
    ) -> None:
        from blacknode.packages import install_prerequisites, load_package

        package_dir = self.root / spec["name"]
        info = load_package(package_dir)
        components = {
            str(value)
            for value in spec.get("components", [])
        }
        components.update(
            str(value)
            for value in getattr(info, "enabled_components", [])
        )
        adapters = {
            (item["component"], item["adapter"])
            for item in spec.get("adapters", [])
        }
        for value in getattr(info, "enabled_adapters", []):
            component, separator, adapter = str(value).partition("/")
            if separator and component and adapter:
                adapters.add((component, adapter))
        try:
            workspaces = declared_ros2_workspaces(
                package_dir,
                components=sorted(components),
                adapters=sorted(adapters),
            )
        except PackageWorkspaceError as exc:
            raise PackageSyncError(f"{spec['name']} has invalid {exc}") from exc
        missing = [
            workspace_setup_path(workspace)
            for workspace in workspaces
            if not workspace_setup_path(workspace).is_file()
        ]
        if not missing:
            return

        relative = [
            str(path.relative_to(package_dir))
            for path in missing
        ]
        messages.append(
            f"Repairing {spec['name']} ROS 2 workspace setup: "
            + ", ".join(relative)
        )
        install_prerequisites(package_dir, progress=messages.append)
        remaining = [path for path in missing if not path.is_file()]
        if remaining:
            detail = ", ".join(
                str(path.relative_to(package_dir))
                for path in remaining
            )
            raise PackageSyncError(
                f"{spec['name']} setup did not build its declared ROS 2 "
                f"workspace overlays: {detail}"
            )

    @staticmethod
    def _activate_requirements(
        spec: dict[str, Any],
        messages: list[str],
    ) -> list[dict[str, str]]:
        from blacknode.packages import ensure_adapter_enabled, ensure_component_enabled

        package_name = spec["name"]
        activated: list[dict[str, str]] = []
        for component_name in spec.get("components", []):
            messages.append(f"Activating {package_name}/{component_name}")
            ensure_component_enabled(
                package_name,
                component_name,
                progress=messages.append,
            )
            activated.append({
                "package": package_name,
                "component": component_name,
                "adapter": "",
            })
        for adapter in spec.get("adapters", []):
            component_name = adapter["component"]
            adapter_name = adapter["adapter"]
            messages.append(
                f"Activating {package_name}/{component_name}@{adapter_name}"
            )
            ensure_adapter_enabled(
                package_name,
                component_name,
                adapter_name,
                progress=messages.append,
            )
            activated.append({
                "package": package_name,
                "component": component_name,
                "adapter": adapter_name,
            })
        return activated

    def _load_existing(
        self,
        spec: dict[str, Any],
        package_dir: Path,
        messages: list[str],
    ):
        from blacknode.packages import install_prerequisites, load_package

        name = spec["name"]
        manifest = package_dir / "blacknode-package.toml"
        if not manifest.is_file():
            raise PackageSyncError(
                f"{name} exists at {package_dir} but has no blacknode-package.toml"
            )
        info = load_package(package_dir)
        if info.name != name:
            raise PackageSyncError(
                f"package folder {name} declares a different name: {info.name}"
            )
        requested_version = spec.get("version", "")
        update_requested = bool(spec.get("update"))
        if update_requested or (
            requested_version and info.version != requested_version
        ):
            update_target = requested_version or "latest revision"
            self._update_existing(name, package_dir, update_target, messages)
            install_prerequisites(package_dir, progress=messages.append)
            info = load_package(package_dir)
        elif not info.ok:
            messages.append(f"Repairing prerequisites for {name}")
            install_prerequisites(package_dir, progress=messages.append)
            info = load_package(package_dir)
        if not info.ok:
            raise PackageSyncError(f"{name} could not load: {info.error}")
        if requested_version and info.version != requested_version:
            raise PackageSyncError(
                f"{name} is version {info.version or 'unknown'} after update; "
                f"deployment requires {requested_version}"
            )
        return info

    @staticmethod
    def _update_existing(
        name: str,
        package_dir: Path,
        requested_version: str,
        messages: list[str],
    ) -> None:
        if not (package_dir / ".git").is_dir():
            raise PackageSyncError(
                f"{name} {requested_version} is required, but the existing package "
                "is not a Git checkout"
            )
        status = subprocess.run(
            ["git", "-C", str(package_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if status.returncode != 0:
            raise PackageSyncError(
                f"could not inspect {name}: {status.stderr.strip() or status.stdout.strip()}"
            )
        if status.stdout.strip():
            messages.append(
                f"Preserving local changes before updating {name}"
            )
            preserved = subprocess.run(
                [
                    "git",
                    "-C",
                    str(package_dir),
                    "stash",
                    "push",
                    "--include-untracked",
                    "--message",
                    f"blacknode-runtime package sync before {requested_version}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if preserved.returncode != 0:
                raise PackageSyncError(
                    f"{name} has local changes and they could not be preserved: "
                    f"{preserved.stderr.strip() or preserved.stdout.strip()}"
                )
            clean = subprocess.run(
                ["git", "-C", str(package_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if clean.returncode != 0 or clean.stdout.strip():
                detail = (
                    clean.stderr.strip()
                    or clean.stdout.strip()
                    or "checkout is still modified after git stash"
                )
                raise PackageSyncError(
                    f"{name} local changes were preserved, but its checkout "
                    f"could not be cleaned: {detail}"
                )
            stash = subprocess.run(
                [
                    "git",
                    "-C",
                    str(package_dir),
                    "stash",
                    "list",
                    "-1",
                    "--format=%gd",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            stash_ref = (
                stash.stdout.strip()
                if stash.returncode == 0 and stash.stdout.strip()
                else "the package Git stash"
            )
            messages.append(
                f"Preserved {name} local changes in {stash_ref}; "
                f"recover them with git -C {package_dir} stash pop"
            )
        messages.append(f"Updating {name} to version {requested_version}")
        update = subprocess.run(
            ["git", "-C", str(package_dir), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if update.returncode != 0:
            raise PackageSyncError(
                f"{name} could not fast-forward: "
                f"{update.stderr.strip() or update.stdout.strip()}"
            )

    def _install(self, spec: dict[str, str], messages: list[str]):
        from blacknode.packages import install_from_git

        result = install_from_git(
            spec["source"],
            root=self.root,
            install_deps=True,
            progress=messages.append,
        )
        info = result.get("package")
        if not result.get("ok") or not isinstance(info, dict):
            raise PackageSyncError(
                f"{spec['name']} installation failed: "
                f"{result.get('error') or 'unknown package installation error'}"
            )
        if info.get("name") != spec["name"]:
            raise PackageSyncError(
                f"package source declared {info.get('name')!r}, expected {spec['name']!r}"
            )
        requested_version = spec.get("version", "")
        if requested_version and str(info.get("version") or "") != requested_version:
            raise PackageSyncError(
                f"{spec['name']} installed version {info.get('version') or 'unknown'}; "
                f"deployment requires {requested_version}"
            )
        return info

    @staticmethod
    def _summary(info) -> dict[str, Any]:
        if isinstance(info, dict):
            name = info.get("name")
            version = info.get("version")
            node_types = info.get("node_types")
        else:
            name = info.name
            version = info.version
            node_types = info.node_types
        return {
            "name": str(name),
            "version": str(version or ""),
            "node_types": sorted(str(item) for item in (node_types or [])),
        }

    @staticmethod
    def _package_spec(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PackageSyncError("each package must be an object")
        name = str(value.get("name") or "").strip().lower()
        source = str(value.get("git_url") or value.get("source") or "").strip()
        version = str(value.get("version") or "").strip()
        update = value.get("update", False)
        if not _PACKAGE_NAME_RE.fullmatch(name):
            raise PackageSyncError(f"invalid package name: {name or '(empty)'}")
        if not source:
            raise PackageSyncError(f"{name} has no package source")
        if len(version) > 64 or any(character.isspace() for character in version):
            raise PackageSyncError(f"{name} has an invalid requested version")
        if not isinstance(update, bool):
            raise PackageSyncError(f"{name} update must be a boolean")

        parsed = urlsplit(source)
        if parsed.scheme != "https" or not parsed.hostname:
            raise PackageSyncError(f"{name} source must be an HTTPS Git URL")
        if parsed.username or parsed.password:
            raise PackageSyncError(f"{name} source must not contain credentials")
        source_name = parsed.path.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        if source_name != name:
            raise PackageSyncError(
                f"{name} source repository name must also be {name}"
            )

        raw_components = value.get("components", [])
        if not isinstance(raw_components, list):
            raise PackageSyncError(f"{name} components must be a list")
        components = sorted({
            str(component).strip().lower()
            for component in raw_components
            if isinstance(component, str) and str(component).strip()
        })
        if any(not _COMPONENT_NAME_RE.fullmatch(component) for component in components):
            raise PackageSyncError(f"{name} contains an invalid component name")

        raw_adapters = value.get("adapters", [])
        if not isinstance(raw_adapters, list):
            raise PackageSyncError(f"{name} adapters must be a list")
        adapters: set[tuple[str, str]] = set()
        for raw_adapter in raw_adapters:
            if not isinstance(raw_adapter, dict):
                raise PackageSyncError(f"{name} adapters must contain objects")
            component = str(raw_adapter.get("component") or "").strip().lower()
            adapter = str(raw_adapter.get("adapter") or "").strip().lower()
            if (
                not _COMPONENT_NAME_RE.fullmatch(component)
                or not _COMPONENT_NAME_RE.fullmatch(adapter)
            ):
                raise PackageSyncError(f"{name} contains an invalid adapter requirement")
            adapters.add((component, adapter))

        return {
            "name": name,
            "source": source,
            "version": version,
            "update": update,
            "components": components,
            "adapters": [
                {"component": component, "adapter": adapter}
                for component, adapter in sorted(adapters)
            ],
        }
