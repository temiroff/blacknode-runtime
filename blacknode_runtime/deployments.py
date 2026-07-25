"""Versioned deployment staging, supervision, logs, and rollback."""

from __future__ import annotations

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


MAX_SCRIPT_BYTES = 2 * 1024 * 1024
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
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}

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
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
            }
            self._write_record(record)
            return dict(record)

    def start(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            record = self._refresh(record)
            if record.get("state") == "running":
                return record
            revision = str(record.get("staged_revision") or "")
            script = self.root / deployment_id / "revisions" / revision / "main.py"
            if not revision or not script.is_file():
                raise DeploymentError("deployment has no staged revision")
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
            try:
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
            record.update(
                state="running",
                active_revision=revision,
                pid=process.pid,
                exit_code=None,
                error="",
                updated_at=_now(),
            )
            self._write_record(record)
            return dict(record)

    def stop(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require(deployment_id)
            process = self._processes.pop(deployment_id, None)
            if process is not None and process.poll() is None:
                self._terminate(process)
            record.update(
                state="stopped",
                pid=None,
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

    def stop_all(self) -> None:
        for deployment_id in list(self._processes):
            try:
                self.stop(deployment_id)
            except Exception:
                pass

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("state") != "running":
            return dict(record)
        process = self._processes.get(record["id"])
        if process is None:
            record.update(
                state="stopped",
                pid=None,
                error="runtime restarted; deployment must be started again",
                updated_at=_now(),
            )
            self._write_record(record)
            return dict(record)
        code = process.poll()
        if code is None:
            return dict(record)
        self._processes.pop(record["id"], None)
        record.update(
            state="exited" if code == 0 else "failed",
            pid=None,
            exit_code=code,
            error="" if code == 0 else f"deployment exited with code {code}",
            updated_at=_now(),
        )
        self._write_record(record)
        return dict(record)

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
