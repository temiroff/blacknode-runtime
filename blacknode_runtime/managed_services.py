"""Independent managed ROS 2 services for robot attachments."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diagnostics import inspect_ros2_interfaces
from .environment import RuntimeEnvironmentError, runtime_environment


_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ROS_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_TOPIC_RE = re.compile(r"/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")
_TYPE_RE = re.compile(r"[A-Za-z0-9_]+/msg/[A-Za-z0-9_]+")


class ManagedServiceError(RuntimeError):
    """Raised when a managed service request is invalid or cannot be applied."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ManagedServiceStore:
    """Supervise attachment providers separately from workflow deployments."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            for path in sorted(self.root.glob("*/service.json")):
                record = self._read(path.parent.name)
                if record is not None:
                    records.append(self._refresh(record))
            return records

    def get(self, service_id: str, *, inspect: bool = True) -> dict[str, Any] | None:
        self._validate_id(service_id)
        with self._lock:
            record = self._read(service_id)
            if record is None:
                return None
            record = self._refresh(record)
        if inspect:
            record["diagnostics"] = inspect_ros2_interfaces(
                list(record.get("interfaces") or [])
            )
        return record

    def start(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_id(service_id)
        command = self._validate_command(payload.get("command"))
        interfaces = self._validate_interfaces(payload.get("interfaces"))
        try:
            wait_seconds = max(
                0.0,
                min(float(payload.get("wait_seconds") or 0.0), 30.0),
            )
        except (TypeError, ValueError) as exc:
            raise ManagedServiceError(
                "wait_seconds must be a number from 0 to 30"
            ) from exc
        display_name = str(payload.get("name") or service_id).strip()[:120]
        with self._lock:
            existing = self._read(service_id)
            if existing is not None:
                existing = self._refresh(existing)
                if (
                    existing.get("state") == "running"
                    and existing.get("command") == command
                    and existing.get("interfaces") == interfaces
                ):
                    return self._wait_for_ready(service_id, wait_seconds)
                if existing.get("state") == "running":
                    self._stop_locked(service_id, existing)

            service_dir = self.root / service_id
            service_dir.mkdir(parents=True, exist_ok=True)
            log_path = service_dir / "service.log"
            log = open(log_path, "ab", buffering=0)
            log.write(f"\n=== {_now()} starting {service_id} ===\n".encode())
            try:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=runtime_environment(),
                    start_new_session=os.name != "nt",
                    creationflags=(
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        if os.name == "nt"
                        else 0
                    ),
                )
            except (OSError, RuntimeEnvironmentError) as exc:
                log.close()
                raise ManagedServiceError(
                    f"could not start managed ROS 2 service: {exc}"
                ) from exc
            log.close()
            self._processes[service_id] = process
            record = {
                "id": service_id,
                "name": display_name,
                "kind": "ros2",
                "state": "running",
                "command": command,
                "interfaces": interfaces,
                "pid": process.pid,
                "exit_code": None,
                "error": "",
                "created_at": (
                    existing.get("created_at")
                    if existing
                    else _now()
                ),
                "updated_at": _now(),
            }
            self._write(record)
        return self._wait_for_ready(service_id, wait_seconds)

    def _wait_for_ready(
        self,
        service_id: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        while True:
            record = self.get(service_id)
            if record is None:
                raise ManagedServiceError("managed service disappeared")
            diagnostics = (
                record.get("diagnostics")
                if isinstance(record.get("diagnostics"), dict)
                else {}
            )
            if (
                record.get("state") != "running"
                or diagnostics.get("ok")
                or time.monotonic() >= deadline
            ):
                return record
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def stop(self, service_id: str) -> dict[str, Any]:
        self._validate_id(service_id)
        with self._lock:
            record = self._read(service_id)
            if record is None:
                raise KeyError(service_id)
            return self._stop_locked(service_id, record)

    def logs(self, service_id: str, limit: int = 20000) -> str:
        self._validate_id(service_id)
        limit = max(1, min(int(limit), 200000))
        path = self.root / service_id / "service.log"
        if not path.is_file():
            return ""
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read().decode("utf-8", errors="replace")

    def stop_all(self) -> None:
        with self._lock:
            for service_id in list(self._processes):
                record = self._read(service_id)
                if record is not None:
                    self._stop_locked(service_id, record)

    def _stop_locked(
        self,
        service_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        process = self._processes.pop(service_id, None)
        if process is not None and process.poll() is None:
            self._terminate(process)
        record.update(
            state="stopped",
            pid=None,
            exit_code=process.poll() if process is not None else record.get("exit_code"),
            updated_at=_now(),
        )
        self._write(record)
        return dict(record)

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        service_id = str(record["id"])
        process = self._processes.get(service_id)
        if process is None:
            if record.get("state") == "running":
                record.update(
                    state="stopped",
                    pid=None,
                    error="Runtime restarted; start the attachment service again.",
                    updated_at=_now(),
                )
                self._write(record)
            return dict(record)
        exit_code = process.poll()
        if exit_code is None:
            return dict(record)
        self._processes.pop(service_id, None)
        record.update(
            state="failed" if exit_code else "stopped",
            pid=None,
            exit_code=exit_code,
            error=(
                f"managed ROS 2 service exited with code {exit_code}"
                if exit_code
                else ""
            ),
            updated_at=_now(),
        )
        self._write(record)
        return dict(record)

    def _validate_id(self, service_id: str) -> None:
        if _ID_RE.fullmatch(str(service_id or "")) is None:
            raise ManagedServiceError("managed service id is invalid")

    def _validate_command(self, raw: Any) -> list[str]:
        if not isinstance(raw, dict):
            raise ManagedServiceError("command must be a JSON object")
        verb = str(raw.get("verb") or "").strip()
        package = str(raw.get("package") or "").strip()
        target = str(raw.get("target") or "").strip()
        if verb not in {"run", "launch"}:
            raise ManagedServiceError("ROS 2 command verb must be run or launch")
        if _ROS_NAME_RE.fullmatch(package) is None:
            raise ManagedServiceError("ROS 2 package name is invalid")
        if _ROS_NAME_RE.fullmatch(target) is None:
            raise ManagedServiceError("ROS 2 command target is invalid")
        arguments = raw.get("arguments") or []
        if not isinstance(arguments, list) or len(arguments) > 64:
            raise ManagedServiceError("ROS 2 arguments must be a list of at most 64 values")
        clean_arguments = []
        for argument in arguments:
            value = str(argument)
            if not value or len(value) > 512 or "\x00" in value:
                raise ManagedServiceError("ROS 2 command argument is invalid")
            clean_arguments.append(value)
        return ["ros2", verb, package, target, *clean_arguments]

    def _validate_interfaces(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 32:
            raise ManagedServiceError("interfaces must be a list of at most 32 values")
        interfaces = []
        for item in raw:
            if not isinstance(item, dict):
                raise ManagedServiceError("each interface must be a JSON object")
            topic = str(item.get("topic") or "").strip()
            message_type = str(item.get("type") or "").strip()
            direction = str(item.get("direction") or "publisher").strip().lower()
            if _TOPIC_RE.fullmatch(topic) is None:
                raise ManagedServiceError(f"ROS 2 topic is invalid: {topic}")
            if message_type and _TYPE_RE.fullmatch(message_type) is None:
                raise ManagedServiceError(
                    f"ROS 2 message type is invalid: {message_type}"
                )
            if direction not in {"publisher", "subscriber"}:
                raise ManagedServiceError(
                    "ROS 2 interface direction must be publisher or subscriber"
                )
            interfaces.append({
                "topic": topic,
                "type": message_type,
                "required": bool(item.get("required", True)),
                "direction": direction,
            })
        return interfaces

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
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

    def _read(self, service_id: str) -> dict[str, Any] | None:
        path = self.root / service_id / "service.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedServiceError(
                f"could not read managed service {service_id}"
            ) from exc
        return dict(payload)

    def _write(self, record: dict[str, Any]) -> None:
        path = self.root / str(record["id"]) / "service.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
