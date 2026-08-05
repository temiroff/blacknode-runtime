"""Managed ROS 2 image streams backed by the installed blacknode-ros2 package."""

from __future__ import annotations

import importlib
import re
import threading
from typing import Any, Protocol


_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TOPIC_RE = re.compile(r"/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")


class Ros2ImageStreamError(RuntimeError):
    """Raised when a remote image stream request is invalid or unavailable."""


class Ros2ImageAdapter(Protocol):
    def resolve_message_type(self, topic: str, requested: str) -> str: ...
    def start(self, config: dict[str, Any]) -> dict[str, Any]: ...
    def once(self, config: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, stream_id: str) -> dict[str, Any]: ...
    def stop(self, stream_id: str) -> dict[str, Any]: ...


class BlacknodeRos2ImageAdapter:
    """Lazy adapter so Runtime still loads when image transport is absent."""

    @staticmethod
    def _runtime():
        try:
            import blacknode  # noqa: F401 - triggers extension discovery
            return importlib.import_module("blacknode.pkg.blacknode_ros2.ros2_runtime")
        except (ImportError, ModuleNotFoundError) as exc:
            raise Ros2ImageStreamError(
                "blacknode-ros2 is not installed on this Runtime device"
            ) from exc

    def resolve_message_type(self, topic: str, requested: str) -> str:
        requested = str(requested or "auto").strip().lower()
        if requested in {"raw", "compressed"}:
            return requested
        result = self._runtime().run_ros2(["topic", "type", topic], timeout=15)
        advertised = " ".join(str(result.get("stdout") or "").split())
        if result.get("ok") and "sensor_msgs/msg/CompressedImage" in advertised:
            return "compressed"
        if result.get("ok") and "sensor_msgs/msg/Image" in advertised:
            return "raw"
        raise Ros2ImageStreamError(
            str(result.get("error") or result.get("stderr") or f"{topic} is not an image topic")
        )

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._runtime().start_image_stream(
            stream_id=config["id"],
            topic=config["topic"],
            message_type=config["message_type"],
            host="0.0.0.0",
            port=config["port"],
            max_fps=config["max_fps"],
            max_width=config["max_width"],
            jpeg_quality=config["jpeg_quality"],
        )

    def once(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._runtime().capture_image_snapshot(
            topic=config["topic"],
            message_type=config["message_type"],
            timeout=config["timeout"],
            output_format="jpeg",
            jpeg_quality=config["jpeg_quality"],
        )

    def status(self, stream_id: str) -> dict[str, Any]:
        runtime = self._runtime()
        status = getattr(runtime, "image_stream_status", None)
        if not callable(status):
            raise Ros2ImageStreamError(
                "blacknode-ros2 must be updated for paired-device image streams"
            )
        return status(stream_id)

    def stop(self, stream_id: str) -> dict[str, Any]:
        return self._runtime().stop_image_stream(stream_id)


class Ros2ImageStreamStore:
    """Own remote image stream configuration while blacknode-ros2 owns transport."""

    def __init__(self, adapter: Ros2ImageAdapter | None = None):
        self.adapter = adapter or BlacknodeRos2ImageAdapter()
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = sorted(self._records)
        return [self.status(stream_id) for stream_id in ids]

    def start(self, stream_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._config(stream_id, payload)
        config["message_type"] = self.adapter.resolve_message_type(
            config["topic"], config["message_type"]
        )
        result = dict(self.adapter.start(config) or {})
        result.setdefault("stream_id", stream_id)
        result.setdefault("topic", config["topic"])
        result.setdefault("message_type", config["message_type"])
        with self._lock:
            self._records[stream_id] = config
        return {"id": stream_id, "stream": result}

    def once(self, stream_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._config(stream_id, payload)
        config["message_type"] = self.adapter.resolve_message_type(
            config["topic"], config["message_type"]
        )
        result = dict(self.adapter.once(config) or {})
        result.setdefault("stream_id", stream_id)
        result.setdefault("topic", config["topic"])
        result.setdefault("message_type", config["message_type"])
        return {"id": stream_id, "stream": result}

    def status(self, stream_id: str) -> dict[str, Any]:
        self._validate_id(stream_id)
        with self._lock:
            config = dict(self._records.get(stream_id) or {})
        if not config:
            return {
                "id": stream_id,
                "stream": {
                    "ok": False,
                    "running": False,
                    "stream_id": stream_id,
                    "error": "image stream has not been started on this device",
                },
            }
        result = dict(self.adapter.status(stream_id) or {})
        result.setdefault("topic", config["topic"])
        result.setdefault("message_type", config["message_type"])
        return {"id": stream_id, "stream": result}

    def stop(self, stream_id: str) -> dict[str, Any]:
        self._validate_id(stream_id)
        with self._lock:
            self._records.pop(stream_id, None)
        result = dict(self.adapter.stop(stream_id) or {})
        result.update(stream_id=stream_id, running=False)
        result.setdefault("ok", True)
        return {"id": stream_id, "stream": result}

    def stop_all(self) -> int:
        with self._lock:
            ids = list(self._records)
        for stream_id in ids:
            self.stop(stream_id)
        return len(ids)

    def _config(self, stream_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_id(stream_id)
        topic = str(payload.get("topic") or "").strip()
        if _TOPIC_RE.fullmatch(topic) is None:
            raise Ros2ImageStreamError("ROS 2 image topic is invalid")
        message_type = str(payload.get("message_type") or "auto").strip().lower()
        if message_type not in {"auto", "raw", "compressed"}:
            raise Ros2ImageStreamError("image message type must be auto, raw, or compressed")
        try:
            port = max(0, min(65535, int(payload.get("port") or 0)))
            max_fps = max(0.1, min(60.0, float(payload.get("max_fps") or 10.0)))
            max_width = max(0, min(8192, int(payload.get("max_width") or 960)))
            jpeg_quality = max(1, min(100, int(payload.get("jpeg_quality") or 80)))
            timeout = max(1.0, min(120.0, float(payload.get("timeout") or 15.0)))
        except (TypeError, ValueError) as exc:
            raise Ros2ImageStreamError("image stream options must be numeric") from exc
        return {
            "id": stream_id,
            "topic": topic,
            "message_type": message_type,
            "port": port,
            "max_fps": max_fps,
            "max_width": max_width,
            "jpeg_quality": jpeg_quality,
            "timeout": timeout,
        }

    @staticmethod
    def _validate_id(stream_id: str) -> None:
        if _ID_RE.fullmatch(str(stream_id or "")) is None:
            raise Ros2ImageStreamError("image stream id is invalid")
