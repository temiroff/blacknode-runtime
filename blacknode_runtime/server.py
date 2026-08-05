"""Authenticated HTTP API for the Blacknode device runtime."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import authorization_matches
from .config import RuntimeConfig
from .deployments import DeploymentError, DeploymentStore
from .diagnostics import ros2_diagnostics
from .managed_services import ManagedServiceError, ManagedServiceStore
from .manifest import runtime_manifest
from .package_manager import PackageManager, PackageSyncError
from .ros2_images import Ros2ImageStreamError, Ros2ImageStreamStore
from .ros2_streams import Ros2TopicStreamError, Ros2TopicStreamStore


MAX_REQUEST_BYTES = 5 * 1024 * 1024
_DEPLOYMENT_PATH = re.compile(
    r"^/deployments/([a-z0-9-]+)(?:/(start|stop|logs|rollback|telemetry|workflow|control))?$"
)
_SERVICE_PATH = re.compile(
    r"^/services/([a-z0-9-]+)(?:/(start|stop|logs))?$"
)
_ROS2_TOPIC_PATH = re.compile(
    r"^/ros2/topics/([a-z0-9-]+)(?:/(start|once|stop))?$"
)
_ROS2_IMAGE_PATH = re.compile(
    r"^/ros2/images/([a-z0-9-]+)(?:/(start|once|stop))?$"
)


class RuntimeRequestHandler(BaseHTTPRequestHandler):
    config: RuntimeConfig
    store: DeploymentStore
    auth_token: str
    package_manager: PackageManager | None
    service_store: ManagedServiceStore
    ros2_topic_store: Ros2TopicStreamStore
    ros2_image_store: Ros2ImageStreamStore

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self) -> bool:
        if authorization_matches(self.headers.get("Authorization"), self.auth_token):
            return True
        self._send(401, {"ok": False, "error": "authentication required"}, {
            "WWW-Authenticate": "Bearer",
        })
        return False

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DeploymentError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise DeploymentError("request body is empty or exceeds the 5 MB limit")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise DeploymentError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise DeploymentError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {
                "ok": True,
                "service": "blacknode-runtime",
                "auth_required": True,
                "protocol_version": 1,
            })
            return
        if not self._require_auth():
            return
        if path == "/manifest":
            self._send(200, runtime_manifest(self.config))
            return
        if path == "/deployments":
            self._send(200, {"deployments": self.store.list()})
            return
        if path == "/services":
            self._send(200, {"services": self.service_store.list()})
            return
        if path == "/diagnostics/ros2":
            self._send(200, ros2_diagnostics())
            return
        if path == "/ros2/topics":
            self._send(200, {"streams": self.ros2_topic_store.list()})
            return
        if path == "/ros2/images":
            self._send(200, {"streams": self.ros2_image_store.list()})
            return
        image_match = _ROS2_IMAGE_PATH.fullmatch(path)
        if image_match:
            stream_id, action = image_match.groups()
            if action is not None:
                self._send(405, {"ok": False, "error": "method not allowed"})
                return
            try:
                self._send(200, self.ros2_image_store.status(stream_id))
            except Ros2ImageStreamError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            return
        topic_match = _ROS2_TOPIC_PATH.fullmatch(path)
        if topic_match:
            service_id, action = topic_match.groups()
            if action is not None:
                self._send(405, {"ok": False, "error": "method not allowed"})
                return
            try:
                self._send(200, self.ros2_topic_store.status(service_id))
            except Ros2TopicStreamError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            return
        service_match = _SERVICE_PATH.fullmatch(path)
        if service_match:
            service_id, action = service_match.groups()
            try:
                if action == "logs":
                    query = parse_qs(urlsplit(self.path).query)
                    limit = int((query.get("limit") or ["20000"])[0])
                    self._send(200, {
                        "id": service_id,
                        "logs": self.service_store.logs(service_id, limit),
                    })
                elif action is None:
                    record = self.service_store.get(service_id)
                    if record is None:
                        self._send(404, {"ok": False, "error": "service not found"})
                    else:
                        self._send(200, record)
                else:
                    self._send(405, {"ok": False, "error": "method not allowed"})
            except (ManagedServiceError, ValueError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            return
        match = _DEPLOYMENT_PATH.fullmatch(path)
        if not match:
            self._send(404, {"ok": False, "error": "not found"})
            return
        deployment_id, action = match.groups()
        try:
            if action == "logs":
                query = parse_qs(urlsplit(self.path).query)
                limit = int((query.get("limit") or ["20000"])[0])
                self._send(200, {"id": deployment_id, "logs": self.store.logs(deployment_id, limit)})
            elif action == "telemetry":
                query = parse_qs(urlsplit(self.path).query)
                stream = str((query.get("stream") or ["robot-state"])[0]).strip()
                self._send(200, self.store.telemetry(deployment_id, stream))
            elif action == "workflow":
                query = parse_qs(urlsplit(self.path).query)
                revision = str((query.get("revision") or [""])[0]).strip()
                self._send(200, self.store.workflow(deployment_id, revision))
            elif action is None:
                record = self.store.get(deployment_id)
                if record is None:
                    self._send(404, {"ok": False, "error": "deployment not found"})
                else:
                    self._send(200, record)
            else:
                self._send(405, {"ok": False, "error": "method not allowed"})
        except KeyError:
            self._send(404, {"ok": False, "error": "deployment not found"})
        except (DeploymentError, ValueError) as exc:
            self._send(400, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        try:
            payload = self._json_body()
            if path == "/packages/sync":
                if self.package_manager is None:
                    self._send(503, {"ok": False, "error": "package sync is not configured"})
                else:
                    self._send(200, self.package_manager.sync(payload))
                return
            if path == "/deployments":
                self._send(201, self.store.stage(payload))
                return
            image_match = _ROS2_IMAGE_PATH.fullmatch(path)
            if image_match:
                stream_id, action = image_match.groups()
                if action == "start":
                    result = self.ros2_image_store.start(stream_id, payload)
                elif action == "once":
                    result = self.ros2_image_store.once(stream_id, payload)
                elif action == "stop":
                    result = self.ros2_image_store.stop(stream_id)
                else:
                    self._send(405, {"ok": False, "error": "method not allowed"})
                    return
                self._send(200, result)
                return
            topic_match = _ROS2_TOPIC_PATH.fullmatch(path)
            if topic_match:
                service_id, action = topic_match.groups()
                if action == "start":
                    result = self.ros2_topic_store.start(service_id, payload)
                elif action == "once":
                    result = self.ros2_topic_store.once(service_id, payload)
                elif action == "stop":
                    result = self.ros2_topic_store.stop(service_id)
                else:
                    self._send(405, {"ok": False, "error": "method not allowed"})
                    return
                self._send(200, result)
                return
            service_match = _SERVICE_PATH.fullmatch(path)
            if service_match:
                service_id, action = service_match.groups()
                if action == "start":
                    result = self.service_store.start(service_id, payload)
                elif action == "stop":
                    result = self.service_store.stop(service_id)
                else:
                    self._send(405, {"ok": False, "error": "method not allowed"})
                    return
                self._send(200, result)
                return
            match = _DEPLOYMENT_PATH.fullmatch(path)
            if not match:
                self._send(404, {"ok": False, "error": "not found"})
                return
            deployment_id, action = match.groups()
            if action == "start":
                result = self.store.start(deployment_id)
            elif action == "stop":
                result = self.store.stop(deployment_id)
            elif action == "rollback":
                result = self.store.rollback(deployment_id, start=bool(payload.get("start")))
            elif action == "control":
                command = str(payload.get("command") or "").strip().lower()
                if command not in {"arm", "disarm"}:
                    raise DeploymentError(
                        "deployment control command must be arm or disarm"
                    )
                result = self.store.set_motion_armed(
                    deployment_id,
                    command == "arm",
                )
            else:
                self._send(405, {"ok": False, "error": "method not allowed"})
                return
            self._send(200, result)
        except KeyError:
            self._send(404, {"ok": False, "error": "deployment not found"})
        except DeploymentError as exc:
            self._send(409, {"ok": False, "error": str(exc)})
        except ManagedServiceError as exc:
            self._send(409, {"ok": False, "error": str(exc)})
        except PackageSyncError as exc:
            self._send(409, {"ok": False, "error": str(exc)})
        except Ros2TopicStreamError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Ros2ImageStreamError as exc:
            self._send(400, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        match = _DEPLOYMENT_PATH.fullmatch(urlsplit(self.path).path)
        if not match or match.group(2) is not None:
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            if not self.store.delete(match.group(1)):
                self._send(404, {"ok": False, "error": "deployment not found"})
            else:
                self._send(200, {"ok": True, "id": match.group(1)})
        except DeploymentError as exc:
            self._send(409, {"ok": False, "error": str(exc)})


def create_server(
    config: RuntimeConfig,
    store: DeploymentStore,
    auth_token: str,
    *,
    package_manager: PackageManager | None = None,
    service_store: ManagedServiceStore | None = None,
    ros2_topic_store: Ros2TopicStreamStore | None = None,
    ros2_image_store: Ros2ImageStreamStore | None = None,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    if not auth_token:
        raise RuntimeError("runtime service authentication is required")
    bound_service_store = service_store or ManagedServiceStore(
        Path(config.state_dir) / "services"
    )
    bound_ros2_topic_store = ros2_topic_store or Ros2TopicStreamStore()
    bound_ros2_image_store = ros2_image_store or Ros2ImageStreamStore()
    handler = type(
        "BoundRuntimeRequestHandler",
        (RuntimeRequestHandler,),
        {
            "config": config,
            "store": store,
            "auth_token": auth_token,
            "package_manager": package_manager,
            "service_store": bound_service_store,
            "ros2_topic_store": bound_ros2_topic_store,
            "ros2_image_store": bound_ros2_image_store,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    config: RuntimeConfig,
    store: DeploymentStore,
    auth_token: str,
    *,
    package_manager: PackageManager | None = None,
    service_store: ManagedServiceStore | None = None,
    ros2_topic_store: Ros2TopicStreamStore | None = None,
    ros2_image_store: Ros2ImageStreamStore | None = None,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> None:
    server = create_server(
        config,
        store,
        auth_token,
        package_manager=package_manager,
        service_store=service_store,
        ros2_topic_store=ros2_topic_store,
        ros2_image_store=ros2_image_store,
        host=host,
        port=port,
    )
    print(f"blacknode-runtime listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.RequestHandlerClass.service_store.stop_all()
        server.RequestHandlerClass.ros2_topic_store.stop_all()
        server.RequestHandlerClass.ros2_image_store.stop_all()
        store.stop_all()
