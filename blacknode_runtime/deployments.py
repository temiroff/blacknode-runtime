"""Versioned deployment staging, supervision, logs, and rollback."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diagnostics import is_ros2_armed_control_topic, publish_ros2_armed_control
from .telemetry import DeploymentTelemetryReceiver


MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
DEFAULT_TELEMETRY_STARTUP_GRACE_SECONDS = 15.0
DEFAULT_TELEMETRY_STALE_FAILURE_SECONDS = 5.0
DEFAULT_TELEMETRY_WATCHDOG_INTERVAL_SECONDS = 0.5
_ID_RE = re.compile(r"[^a-z0-9]+")
_PROJECT_ID_RE = re.compile(r"[a-z0-9-]{1,60}")
_WORKFLOW_SLUG_RE = re.compile(r"[a-zA-Z0-9_-]{1,60}")


class DeploymentError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return _ID_RE.sub("-", value.strip().lower()).strip("-")[:48] or "deployment"


class DeploymentStore:
    def __init__(
        self,
        root: Path,
        *,
        telemetry_startup_grace_seconds: float = (
            DEFAULT_TELEMETRY_STARTUP_GRACE_SECONDS
        ),
        telemetry_stale_failure_seconds: float = (
            DEFAULT_TELEMETRY_STALE_FAILURE_SECONDS
        ),
        telemetry_watchdog_interval_seconds: float = (
            DEFAULT_TELEMETRY_WATCHDOG_INTERVAL_SECONDS
        ),
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._telemetry: dict[str, DeploymentTelemetryReceiver] = {}
        self._telemetry_started: dict[str, float] = {}
        self._watchdogs: dict[str, threading.Timer] = {}
        self.telemetry_startup_grace_seconds = max(
            0.1,
            float(telemetry_startup_grace_seconds),
        )
        self.telemetry_stale_failure_seconds = max(
            0.1,
            float(telemetry_stale_failure_seconds),
        )
        self.telemetry_watchdog_interval_seconds = max(
            0.02,
            float(telemetry_watchdog_interval_seconds),
        )

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            for path in sorted(self.root.glob("*/deployment.json")):
                record = self._read(path.parent.name)
                if record is not None:
                    records.append(self._refresh(record))
            return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)

    def get(self, deployment_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._read(deployment_id)
            return self._refresh(record) if record is not None else None

    def stage(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "Deployment").strip() or "Deployment"
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            raise DeploymentError("deployment script is required")
        encoded = script.encode("utf-8")
        if len(encoded) > MAX_SCRIPT_BYTES:
            raise DeploymentError("deployment script exceeds the 2 MB limit")
        try:
            compile(script, "main.py", "exec")
        except SyntaxError as exc:
            raise DeploymentError(
                f"deployment script does not compile: line {exc.lineno}: {exc.msg}"
            ) from exc
        manifest = payload.get("manifest") or {}
        if not isinstance(manifest, dict):
            raise DeploymentError("deployment manifest must be an object")
        workflow = payload.get("workflow")
        if workflow is not None:
            self._validate_workflow_snapshot(workflow)

        requested_id = str(payload.get("deployment_id") or "").strip()
        deployment_id = _slug(requested_id) if requested_id else f"{_slug(name)}-{uuid.uuid4().hex[:8]}"
        revision = hashlib.sha256(encoded).hexdigest()[:16]
        with self._lock:
            existing = self._read(deployment_id)
            if existing is not None:
                existing = self._refresh(existing)
            if existing and existing.get("state") == "running":
                raise DeploymentError("stop the running deployment before staging a revision")
            project_id, workflow_slug = self._deployment_owner(manifest, existing)
            if project_id:
                manifest = {
                    **manifest,
                    "project_id": project_id,
                    "workflow_slug": workflow_slug,
                }
            directory = self.root / deployment_id
            revision_dir = directory / "revisions" / revision
            revision_dir.mkdir(parents=True, exist_ok=True)
            self._write_text(revision_dir / "main.py", script)
            self._write_json(revision_dir / "manifest.json", manifest)
            if workflow is not None:
                self._write_json(revision_dir / "workflow.json", workflow)
            now = _now()
            revisions = list(existing.get("revisions", [])) if existing else []
            if revision not in revisions:
                revisions.append(revision)
            target_device_id = str(
                manifest.get("target_device_id")
                or (existing or {}).get("target_device_id")
                or ""
            ).strip()
            record = {
                "id": deployment_id,
                "name": name,
                "target_device_id": target_device_id,
                "project_id": project_id,
                "workflow_slug": workflow_slug,
                "state": "staged",
                "staged_revision": revision,
                "active_revision": existing.get("active_revision") if existing else None,
                "revisions": revisions,
                "pid": None,
                "exit_code": None,
                "error": "",
                "telemetry_required": bool(manifest.get("telemetry_required")),
                "motion_control_count": len([
                    item
                    for item in (manifest.get("motion_controls") or [])
                    if (
                        isinstance(item, dict)
                        and item.get("kind") == "ros2_leader_follower"
                        and is_ros2_armed_control_topic(str(item.get("topic") or ""))
                    )
                ]),
                "motion_armed": False,
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
            }
            self._write_record(record)
            return dict(record)

    def workflow(
        self,
        deployment_id: str,
        revision: str = "",
    ) -> dict[str, Any]:
        """Return the graph captured for a revision.

        Runtime 0.3.13+ stores ``workflow.json`` alongside the generated
        script. Older Blacknode exports embedded the same graph in the
        literal ``_WORKFLOW`` assignment, so they remain recoverable without
        executing deployment code.
        """
        with self._lock:
            record = self._require(deployment_id)
            selected_revision = str(
                revision
                or (
                    record.get("active_revision")
                    if record.get("state") == "running"
                    else record.get("staged_revision")
                )
                or record.get("active_revision")
                or ""
            ).strip()
            if not selected_revision or selected_revision not in record.get("revisions", []):
                raise DeploymentError("deployment revision was not found")
            revision_dir = self.root / deployment_id / "revisions" / selected_revision
            workflow_path = revision_dir / "workflow.json"
            if workflow_path.is_file():
                try:
                    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DeploymentError("could not read deployment workflow snapshot") from exc
                source = "snapshot"
            else:
                script_path = revision_dir / "main.py"
                workflow = self._workflow_from_generated_script(script_path)
                source = "generated_script"
            self._validate_workflow_snapshot(workflow)
            return {
                "id": deployment_id,
                "revision": selected_revision,
                "source": source,
                "workflow": workflow,
            }

    def set_motion_armed(
        self,
        deployment_id: str,
        armed: bool,
    ) -> dict[str, Any]:
        """Send one arm-state command to a running deployment's declared gate."""
        with self._lock:
            record = self._refresh(self._require(deployment_id))
            if record.get("state") != "running":
                raise DeploymentError("deployment must be running before it can be armed")
            revision = str(record.get("active_revision") or "").strip()
            manifest_path = (
                self.root
                / deployment_id
                / "revisions"
                / revision
                / "manifest.json"
            )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeploymentError("could not read deployment motion controls") from exc
            controls = [
                item
                for item in (manifest.get("motion_controls") or [])
                if (
                    isinstance(item, dict)
                    and item.get("kind") == "ros2_leader_follower"
                    and is_ros2_armed_control_topic(str(item.get("topic") or ""))
                )
            ]
            if not controls:
                raise DeploymentError(
                    "deployment has no remotely controllable armed gate; stage it again"
                )
            if len(controls) != 1:
                raise DeploymentError(
                    "deployment has multiple armed gates; control them from their workflow nodes"
                )
            control = controls[0]
            result = publish_ros2_armed_control(
                str(control["topic"]),
                bool(armed),
            )
            if not result.get("ok"):
                raise DeploymentError(
                    "deployment arm command failed: "
                    + str(result.get("error") or result.get("stderr") or "unknown ROS 2 error")
                )
            record.update(
                motion_armed=bool(armed),
                updated_at=_now(),
            )
            self._write_record(record)
            return {
                "ok": True,
                "id": deployment_id,
                "armed": bool(armed),
                "topic": str(control["topic"]),
                "node_id": str(control.get("node_id") or ""),
                "deployment": dict(record),
            }

    def start(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            record = self._refresh(record)
            revision = str(record.get("staged_revision") or "")
            script = self.root / deployment_id / "revisions" / revision / "main.py"
            if not revision or not script.is_file():
                raise DeploymentError("deployment has no staged revision")
            target_device_id = str(record.get("target_device_id") or "").strip()
            superseded_deployment_ids: list[str] = []
            if target_device_id:
                for path in sorted(self.root.glob("*/deployment.json")):
                    other_id = path.parent.name
                    if other_id == deployment_id:
                        continue
                    other = self._read(other_id)
                    if other is None:
                        continue
                    other = self._refresh(other)
                    if (
                        other.get("state") == "running"
                        and str(other.get("target_device_id") or "").strip()
                        == target_device_id
                    ):
                        self.stop(other_id)
                        superseded_deployment_ids.append(other_id)
            if record.get("state") == "running":
                record.update(
                    superseded_deployment_ids=superseded_deployment_ids,
                    updated_at=_now(),
                )
                self._write_record(record)
                return dict(record)
            log_path = self.root / deployment_id / "deployment.log"
            log = open(log_path, "ab", buffering=0)
            log.write(f"\n=== {_now()} starting {deployment_id}@{revision} ===\n".encode())
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["BLACKNODE_DEPLOYMENT_ID"] = deployment_id
            env["BLACKNODE_DEPLOYMENT_REVISION"] = revision
            if record.get("project_id"):
                env["BLACKNODE_PROJECT_ID"] = str(record["project_id"])
            if record.get("workflow_slug"):
                env["BLACKNODE_WORKFLOW_SLUG"] = str(record["workflow_slug"])
            self._close_telemetry(deployment_id)
            telemetry: DeploymentTelemetryReceiver | None = None
            try:
                telemetry = DeploymentTelemetryReceiver(deployment_id)
                telemetry.start()
                env.update(telemetry.environment())
                process = subprocess.Popen(
                    [sys.executable, str(script)],
                    cwd=str(script.parent),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        if os.name == "nt" else 0
                    ),
                )
            except Exception as exc:
                if telemetry is not None:
                    telemetry.close()
                log.close()
                record.update(
                    state="failed",
                    error=f"could not start deployment: {type(exc).__name__}: {exc}",
                    updated_at=_now(),
                )
                self._write_record(record)
                raise DeploymentError(record["error"]) from exc
            log.close()
            self._processes[deployment_id] = process
            assert telemetry is not None
            self._telemetry[deployment_id] = telemetry
            self._telemetry_started[deployment_id] = time.monotonic()
            record.update(
                state="running",
                active_revision=revision,
                pid=process.pid,
                exit_code=None,
                error="",
                superseded_deployment_ids=superseded_deployment_ids,
                updated_at=_now(),
            )
            self._write_record(record)
            if record.get("telemetry_required"):
                self._schedule_watchdog(deployment_id)
            return dict(record)

    def stop(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            process = self._processes.pop(deployment_id, None)
            if process is not None and process.poll() is None:
                self._terminate(process)
            self._cancel_watchdog(deployment_id)
            self._close_telemetry(deployment_id)
            record.update(
                state="stopped",
                pid=None,
                motion_armed=False,
                exit_code=process.poll() if process is not None else record.get("exit_code"),
                updated_at=_now(),
            )
            self._write_record(record)
            return dict(record)

    def rollback(self, deployment_id: str, *, start: bool = False) -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            if record.get("state") == "running":
                record = self.stop(deployment_id)
            revisions = list(record.get("revisions") or [])
            current = str(record.get("staged_revision") or record.get("active_revision") or "")
            if current not in revisions or revisions.index(current) == 0:
                raise DeploymentError("deployment has no previous revision")
            previous = revisions[revisions.index(current) - 1]
            record.update(
                staged_revision=previous,
                state="staged",
                pid=None,
                error="",
                updated_at=_now(),
            )
            self._write_record(record)
        return self.start(deployment_id) if start else dict(record)

    def delete(self, deployment_id: str) -> bool:
        with self._lock:
            record = self._read(deployment_id)
            if record is None:
                return False
            if self._refresh(record).get("state") == "running":
                raise DeploymentError("stop the deployment before deleting it")
            import shutil
            shutil.rmtree(self.root / deployment_id)
            self._processes.pop(deployment_id, None)
            self._close_telemetry(deployment_id)
            return True

    def logs(self, deployment_id: str, limit: int = 20000) -> str:
        with self._lock:
            self._require(deployment_id)
            path = self.root / deployment_id / "deployment.log"
            if not path.exists():
                return ""
            with open(path, "rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - max(512, min(limit, 200000))))
                return handle.read().decode("utf-8", errors="replace")

    def telemetry(self, deployment_id: str, stream: str = "robot-state") -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            record = self._refresh(record)
            receiver = self._telemetry.get(deployment_id)
            if receiver is None:
                return {
                    "available": False,
                    "deployment_id": deployment_id,
                    "stream": stream,
                    "stale": True,
                    "state": record.get("state"),
                    "message": (
                        "Deployment telemetry is unavailable because the deployment is not running."
                        if record.get("state") != "running"
                        else "Waiting for the deployment telemetry receiver."
                    ),
                }
            return {
                "state": record.get("state"),
                **receiver.latest(stream),
            }

    def stop_all(self) -> None:
        for deployment_id in list(self._processes):
            try:
                self.stop(deployment_id)
            except Exception:
                pass
        for deployment_id in list(self._telemetry):
            self._close_telemetry(deployment_id)
        for deployment_id in list(self._watchdogs):
            self._cancel_watchdog(deployment_id)

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("state") != "running":
            return dict(record)
        process = self._processes.get(record["id"])
        if process is None:
            self._cancel_watchdog(record["id"])
            self._close_telemetry(record["id"])
            record.update(
                state="stopped",
                pid=None,
                motion_armed=False,
                error="runtime restarted; deployment must be started again",
                updated_at=_now(),
            )
            self._write_record(record)
            return dict(record)
        code = process.poll()
        if code is None:
            telemetry_failure = self._telemetry_failure(record)
            if telemetry_failure:
                self._processes.pop(record["id"], None)
                self._terminate(process)
                self._cancel_watchdog(record["id"])
                self._close_telemetry(record["id"])
                record.update(
                    state="failed",
                    pid=None,
                    motion_armed=False,
                    exit_code=process.poll(),
                    error=telemetry_failure,
                    updated_at=_now(),
                )
                self._write_record(record)
            return dict(record)
        self._processes.pop(record["id"], None)
        self._cancel_watchdog(record["id"])
        self._close_telemetry(record["id"])
        record.update(
            state="exited" if code == 0 else "failed",
            pid=None,
            motion_armed=False,
            exit_code=code,
            error="" if code == 0 else f"deployment exited with code {code}",
            updated_at=_now(),
        )
        self._write_record(record)
        return dict(record)

    def _close_telemetry(self, deployment_id: str) -> None:
        receiver = self._telemetry.pop(deployment_id, None)
        self._telemetry_started.pop(deployment_id, None)
        if receiver is not None:
            receiver.close()

    def _telemetry_failure(self, record: dict[str, Any]) -> str:
        if not bool(record.get("telemetry_required")):
            return ""
        deployment_id = str(record.get("id") or "")
        receiver = self._telemetry.get(deployment_id)
        started = self._telemetry_started.get(deployment_id)
        if receiver is None or started is None:
            return "required robot telemetry receiver is unavailable"
        sample = receiver.latest()
        if not sample.get("available"):
            age = max(0.0, time.monotonic() - started)
            if age > self.telemetry_startup_grace_seconds:
                return (
                    "required robot telemetry did not start within "
                    f"{self.telemetry_startup_grace_seconds:g}s"
                )
            return ""
        age = float(sample.get("age_seconds") or 0.0)
        if age > self.telemetry_stale_failure_seconds:
            return (
                "required robot telemetry became stale "
                f"({age:.2f}s without a fresh sample)"
            )
        return ""

    def _schedule_watchdog(self, deployment_id: str) -> None:
        self._cancel_watchdog(deployment_id)
        timer = threading.Timer(
            self.telemetry_watchdog_interval_seconds,
            self._watchdog_tick,
            args=(deployment_id,),
        )
        timer.daemon = True
        self._watchdogs[deployment_id] = timer
        timer.start()

    def _cancel_watchdog(self, deployment_id: str) -> None:
        timer = self._watchdogs.pop(deployment_id, None)
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _watchdog_tick(self, deployment_id: str) -> None:
        with self._lock:
            self._watchdogs.pop(deployment_id, None)
            record = self._read(deployment_id)
            if record is None:
                return
            refreshed = self._refresh(record)
            if refreshed.get("state") == "running":
                self._schedule_watchdog(deployment_id)

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def _require(self, deployment_id: str) -> dict[str, Any]:
        record = self._read(deployment_id)
        if record is None:
            raise KeyError(deployment_id)
        return record

    def _read(self, deployment_id: str) -> dict[str, Any] | None:
        path = self.root / deployment_id / "deployment.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(f"could not read deployment {deployment_id}") from exc
        record = dict(payload)
        # Additive ownership fields keep pre-0.3.8 deployment records readable
        # and make their unassigned state explicit to API consumers.
        record.setdefault("project_id", "")
        record.setdefault("workflow_slug", "")
        record.setdefault("telemetry_required", False)
        record.setdefault("superseded_deployment_ids", [])
        record.setdefault("motion_armed", False)
        record.setdefault("motion_control_count", 0)
        return record

    @staticmethod
    def _deployment_owner(
        manifest: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> tuple[str, str]:
        project_id = str(manifest.get("project_id") or "").strip()
        workflow_slug = str(manifest.get("workflow_slug") or "").strip()
        if bool(project_id) != bool(workflow_slug):
            raise DeploymentError(
                "deployment ownership requires both project_id and workflow_slug"
            )
        if project_id and not _PROJECT_ID_RE.fullmatch(project_id):
            raise DeploymentError("deployment project_id is invalid")
        if workflow_slug and not _WORKFLOW_SLUG_RE.fullmatch(workflow_slug):
            raise DeploymentError("deployment workflow_slug is invalid")

        existing_project = str((existing or {}).get("project_id") or "").strip()
        existing_workflow = str((existing or {}).get("workflow_slug") or "").strip()
        if project_id and existing_project and project_id != existing_project:
            raise DeploymentError(
                f"deployment belongs to project '{existing_project}'; "
                "stage a new deployment to change projects"
            )
        if workflow_slug and existing_workflow and workflow_slug != existing_workflow:
            raise DeploymentError(
                f"deployment belongs to workflow '{existing_workflow}'; "
                "stage a new deployment to change workflows"
            )
        return (
            project_id or existing_project,
            workflow_slug or existing_workflow,
        )

    def _write_record(self, record: dict[str, Any]) -> None:
        self._write_json(self.root / record["id"] / "deployment.json", record)

    @staticmethod
    def _validate_workflow_snapshot(workflow: Any) -> None:
        if not isinstance(workflow, dict):
            raise DeploymentError("deployment workflow must be an object")
        if workflow.get("kind") != "blacknode.workflow":
            raise DeploymentError("deployment workflow kind must be blacknode.workflow")
        if workflow.get("schema_version") != 1:
            raise DeploymentError("deployment workflow schema_version must be 1")
        if not isinstance(workflow.get("node_meta"), dict):
            raise DeploymentError("deployment workflow node_meta must be an object")
        if not isinstance(workflow.get("edges"), list):
            raise DeploymentError("deployment workflow edges must be a list")
        try:
            encoded = json.dumps(workflow, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DeploymentError("deployment workflow must be JSON serializable") from exc
        if len(encoded) > MAX_WORKFLOW_BYTES:
            raise DeploymentError("deployment workflow exceeds the 2 MB limit")

    @staticmethod
    def _workflow_from_generated_script(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise DeploymentError("deployment script was not found")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename="main.py")
        except (OSError, SyntaxError) as exc:
            raise DeploymentError("could not inspect the generated deployment script") from exc
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if not any(
                isinstance(target, ast.Name) and target.id == "_WORKFLOW"
                for target in targets
            ):
                continue
            try:
                value = ast.literal_eval(statement.value)
            except (TypeError, ValueError) as exc:
                raise DeploymentError(
                    "the generated deployment script has no readable workflow snapshot"
                ) from exc
            if isinstance(value, dict):
                return value
        raise DeploymentError(
            "this deployment predates recoverable workflow snapshots; stage it again"
        )

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
