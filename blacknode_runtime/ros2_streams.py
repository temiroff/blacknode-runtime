"""Managed ROS 2 topic streams backed by the installed blacknode-ros2 package."""

from __future__ import annotations

import importlib
import re
import threading
from typing import Any, Protocol


_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TOPIC_RE = re.compile(r"/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")
_TYPE_RE = re.compile(r"[A-Za-z0-9_]+/msg/[A-Za-z0-9_]+")
_NODE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Ros2TopicStreamError(RuntimeError):
    """Raised when a remote topic stream request is invalid or unavailable."""


class Ros2TopicAdapter(Protocol):
    def discover_type(self, topic: str) -> str: ...
    def start(self, config: dict[str, Any]) -> dict[str, Any]: ...
    def once(self, config: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, topic: str) -> dict[str, Any]: ...
    def stop(self, topic: str) -> dict[str, Any]: ...
    def outputs(self, status: dict[str, Any], report: str) -> dict[str, Any]: ...


class BlacknodeRos2Adapter:
    """Lazy adapter so Runtime still loads when blacknode-ros2 is absent."""

    @staticmethod
    def _runtime():
        try:
            import blacknode  # noqa: F401 - triggers extension discovery
            return importlib.import_module(
                "blacknode.pkg.blacknode_ros2.ros2_runtime"
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise Ros2TopicStreamError(
                "blacknode-ros2 is not installed on this Runtime device"
            ) from exc

    def discover_type(self, topic: str) -> str:
        runtime = self._runtime()
        result = runtime.run_ros2(["topic", "type", topic], timeout=15)
        message_type = next(
            (
                line.strip()
                for line in str(result.get("stdout") or "").splitlines()
                if line.strip()
            ),
            "",
        )
        if not result.get("ok") or not message_type:
            raise Ros2TopicStreamError(
                str(
                    result.get("error")
                    or result.get("stderr")
                    or f"no publisher advertises {topic}"
                )
            )
        return message_type

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime()
        started = runtime.start_topic_subscriber(
            topic=config["topic"],
            message_type=config["message_type"],
            node_name=config["node_name"],
            history=config["history"],
            public_node_type="ROS2",
            stale_after_seconds=config["stale_after_seconds"],
        )
        if not started.get("ok"):
            return started
        return runtime.topic_subscriber_status(config["topic"])

    def once(self, config: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime()
        return runtime.run_topic_subscriber_once(
            topic=config["topic"],
            message_type=config["message_type"],
            node_name=config["node_name"],
            timeout=config["timeout"],
            public_node_type="ROS2",
            stale_after_seconds=config["stale_after_seconds"],
        )

    def status(self, topic: str) -> dict[str, Any]:
        return self._runtime().topic_subscriber_status(topic)

    def stop(self, topic: str) -> dict[str, Any]:
        return self._runtime().stop_topic_subscriber(topic)

    def outputs(self, status: dict[str, Any], report: str) -> dict[str, Any]:
        return self._runtime().ros2_topic_outputs(status, report=report)


def _unavailable_outputs(
    *,
    topic: str,
    message_type: str,
    service_id: str,
    error: str,
) -> dict[str, Any]:
    status = {
        "kind": "blacknode.stream-status",
        "schema_version": 1,
        "stream_id": service_id,
        "state": "unavailable",
        "available": False,
        "worker_alive": False,
        "source_fresh": False,
        "received": 0,
        "last_message_time_ns": 0,
        "age_seconds": None,
        "stale_after_seconds": 2.0,
        "error": error,
    }
    return {
        "running": False,
        "message": {},
        "messages": [],
        "stream": {
            "kind": "blacknode.message-stream",
            "schema_version": 1,
            "stream_id": service_id,
            "protocol": "ros2",
            "state": "unavailable",
            "managed": True,
            "topic": topic,
            "message_type": message_type,
            "backend": "remote",
        },
        "status": status,
        "received": 0,
        "backend": "remote",
        "report": f"ROS2 unavailable: {error}",
    }


def _stopped_outputs(service_id: str) -> dict[str, Any]:
    outputs = _unavailable_outputs(
        topic="",
        message_type="",
        service_id=service_id,
        error="",
    )
    outputs["stream"]["state"] = "stopped"
    outputs["status"].update(state="stopped", error="")
    outputs["report"] = "ROS2 topic stream is stopped"
    return outputs


class Ros2TopicStreamStore:
    """Own remote subscriber configuration while blacknode-ros2 owns transport."""

    def __init__(self, adapter: Ros2TopicAdapter | None = None):
        self.adapter = adapter or BlacknodeRos2Adapter()
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = sorted(self._records)
        return [self.status(service_id) for service_id in ids]

    def start(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._config(service_id, payload)
        try:
            if not config["message_type"]:
                config["message_type"] = self.adapter.discover_type(config["topic"])
            snapshot = self.adapter.start(config)
            snapshot = {
                "topic": config["topic"],
                "message_type": config["message_type"],
                "stale_after_seconds": config["stale_after_seconds"],
                **snapshot,
            }
            outputs = self.adapter.outputs(
                snapshot,
                f"ROS2 streaming {config['message_type']} from {config['topic']} on this device",
            )
        except Ros2TopicStreamError as exc:
            outputs = _unavailable_outputs(
                topic=config["topic"],
                message_type=config["message_type"],
                service_id=service_id,
                error=str(exc),
            )
        with self._lock:
            self._records[service_id] = config
        return {"id": service_id, "outputs": outputs}

    def once(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._config(service_id, payload)
        try:
            if not config["message_type"]:
                config["message_type"] = self.adapter.discover_type(config["topic"])
            snapshot = self.adapter.once(config)
            snapshot = {
                "topic": config["topic"],
                "message_type": config["message_type"],
                "stale_after_seconds": config["stale_after_seconds"],
                **snapshot,
            }
            outputs = self.adapter.outputs(
                snapshot,
                f"ROS2 received one message from {config['topic']} on this device",
            )
        except Ros2TopicStreamError as exc:
            outputs = _unavailable_outputs(
                topic=config["topic"],
                message_type=config["message_type"],
                service_id=service_id,
                error=str(exc),
            )
        return {"id": service_id, "outputs": outputs}

    def status(self, service_id: str) -> dict[str, Any]:
        self._validate_id(service_id)
        with self._lock:
            config = dict(self._records.get(service_id) or {})
        if not config:
            return {
                "id": service_id,
                "outputs": _unavailable_outputs(
                    topic="",
                    message_type="",
                    service_id=service_id,
                    error="topic stream has not been started on this device",
                ),
            }
        try:
            snapshot = self.adapter.status(config["topic"])
            snapshot["stale_after_seconds"] = config["stale_after_seconds"]
            outputs = self.adapter.outputs(
                snapshot,
                f"ROS2 status for {config['topic']} on this device",
            )
        except Ros2TopicStreamError as exc:
            outputs = _unavailable_outputs(
                topic=config["topic"],
                message_type=config["message_type"],
                service_id=service_id,
                error=str(exc),
            )
        return {"id": service_id, "outputs": outputs}

    def stop(self, service_id: str) -> dict[str, Any]:
        self._validate_id(service_id)
        with self._lock:
            config = self._records.pop(service_id, None)
        if not config:
            return {
                "id": service_id,
                "outputs": _stopped_outputs(service_id),
            }
        try:
            snapshot = self.adapter.stop(config["topic"])
            snapshot.update(
                running=False,
                state="stopped",
                topic=config["topic"],
                message_type=config["message_type"],
                stale_after_seconds=config["stale_after_seconds"],
            )
            outputs = self.adapter.outputs(
                snapshot,
                f"ROS2 stopped: {config['topic']} on this device",
            )
        except Ros2TopicStreamError as exc:
            outputs = _unavailable_outputs(
                topic=config["topic"],
                message_type=config["message_type"],
                service_id=service_id,
                error=str(exc),
            )
        return {"id": service_id, "outputs": outputs}

    def stop_all(self) -> int:
        with self._lock:
            ids = list(self._records)
        for service_id in ids:
            self.stop(service_id)
        return len(ids)

    def _config(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_id(service_id)
        topic = str(payload.get("topic") or "").strip()
        message_type = str(payload.get("message_type") or "").strip()
        node_name = str(payload.get("node_name") or "blacknode_ros2_topic").strip().lstrip("/")
        if _TOPIC_RE.fullmatch(topic) is None:
            raise Ros2TopicStreamError("ROS 2 topic is invalid")
        if message_type and _TYPE_RE.fullmatch(message_type) is None:
            raise Ros2TopicStreamError("ROS 2 message type is invalid")
        if _NODE_RE.fullmatch(node_name) is None:
            raise Ros2TopicStreamError("ROS 2 node name is invalid")
        try:
            history = max(1, min(100, int(payload.get("history") or 10)))
            timeout = max(0.1, min(120.0, float(payload.get("timeout") or 10.0)))
            stale = max(0.05, min(120.0, float(payload.get("stale_after_seconds") or 2.0)))
        except (TypeError, ValueError) as exc:
            raise Ros2TopicStreamError("history, timeout, and stale threshold must be numeric") from exc
        return {
            "id": service_id,
            "topic": topic,
            "message_type": message_type,
            "node_name": node_name,
            "history": history,
            "timeout": timeout,
            "stale_after_seconds": stale,
        }

    @staticmethod
    def _validate_id(service_id: str) -> None:
        if _ID_RE.fullmatch(str(service_id or "")) is None:
            raise Ros2TopicStreamError("topic stream id is invalid")
