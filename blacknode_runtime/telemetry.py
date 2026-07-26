"""Local telemetry bridge from deployment processes to the device runtime.

The runtime opens a loopback-only UDP receiver for each running deployment and
passes its address plus a random token through process environment variables.
Drivers can then report high-rate state without exposing another network
listener or coupling the runtime to a particular hardware package.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping


PROTOCOL_VERSION = 1
MAX_DATAGRAM_BYTES = 65_507
DEFAULT_STALE_SECONDS = 2.0
_ADDRESS_ENV = "BLACKNODE_TELEMETRY_UDP"
_TOKEN_ENV = "BLACKNODE_TELEMETRY_TOKEN"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeploymentTelemetryReceiver:
    """Receive and retain the newest sample for each deployment stream."""

    def __init__(self, deployment_id: str, *, stale_seconds: float = DEFAULT_STALE_SECONDS) -> None:
        self.deployment_id = deployment_id
        self.stale_seconds = max(0.1, float(stale_seconds))
        self._token = secrets.token_urlsafe(32)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(0.25)
        host, port = self._socket.getsockname()
        self._address = f"{host}:{port}"
        self._samples: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._receive_loop,
            name=f"blacknode-telemetry-{deployment_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def environment(self) -> dict[str, str]:
        return {
            _ADDRESS_ENV: self._address,
            _TOKEN_ENV: self._token,
        }

    def latest(self, stream: str = "robot-state") -> dict[str, Any]:
        with self._lock:
            sample = dict(self._samples.get(stream) or {})
        if not sample:
            return {
                "available": False,
                "deployment_id": self.deployment_id,
                "stream": stream,
                "stale": True,
                "message": "Waiting for telemetry from the running deployment.",
            }
        age = max(0.0, time.monotonic() - float(sample.pop("_received_monotonic")))
        return {
            "available": True,
            "deployment_id": self.deployment_id,
            "stream": stream,
            "age_seconds": round(age, 3),
            "stale": age > self.stale_seconds,
            **sample,
        }

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._socket.close()
        except OSError:
            pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.6)

    def _receive_loop(self) -> None:
        while not self._closed.is_set():
            try:
                data, peer = self._socket.recvfrom(MAX_DATAGRAM_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break
            if peer[0] not in {"127.0.0.1", "::1"}:
                continue
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            token = str(message.pop("token", ""))
            if not secrets.compare_digest(token, self._token):
                continue
            if message.get("protocol_version") != PROTOCOL_VERSION:
                continue
            if str(message.get("deployment_id") or "") != self.deployment_id:
                continue
            stream = str(message.get("stream") or "").strip()
            payload = message.get("payload")
            if not stream or not isinstance(payload, dict):
                continue
            received_at = _now()
            sample = {
                "sequence": int(message.get("sequence") or 0),
                "sent_at": str(message.get("sent_at") or ""),
                "received_at": received_at,
                "payload": payload,
                "_received_monotonic": time.monotonic(),
            }
            with self._lock:
                self._samples[stream] = sample


class DeploymentTelemetryPublisher:
    """Best-effort telemetry publisher used by drivers inside deployments."""

    def __init__(
        self,
        address: tuple[str, int] | None,
        token: str,
        deployment_id: str,
    ) -> None:
        self.address = address
        self.token = token
        self.deployment_id = deployment_id
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if address else None
        self._sequence = 0
        self._last_positions: dict[str, float] = {}
        self._last_position_time: float | None = None

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> DeploymentTelemetryPublisher:
        env = environment or os.environ
        raw_address = str(env.get(_ADDRESS_ENV) or "").strip()
        token = str(env.get(_TOKEN_ENV) or "")
        deployment_id = str(env.get("BLACKNODE_DEPLOYMENT_ID") or "").strip()
        address: tuple[str, int] | None = None
        if raw_address and token and deployment_id:
            host, separator, raw_port = raw_address.rpartition(":")
            try:
                port = int(raw_port)
                if separator and ipaddress.ip_address(host).is_loopback and 0 < port <= 65_535:
                    address = (host, port)
            except (ValueError, TypeError):
                address = None
        return cls(address, token if address else "", deployment_id if address else "")

    @property
    def enabled(self) -> bool:
        return self._socket is not None and self.address is not None

    def publish(self, stream: str, payload: Mapping[str, Any]) -> bool:
        if not self.enabled or not stream or not isinstance(payload, Mapping):
            return False
        self._sequence += 1
        message = {
            "protocol_version": PROTOCOL_VERSION,
            "token": self.token,
            "deployment_id": self.deployment_id,
            "stream": stream,
            "sequence": self._sequence,
            "sent_at": _now(),
            "payload": dict(payload),
        }
        try:
            encoded = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_DATAGRAM_BYTES:
                return False
            assert self._socket is not None
            assert self.address is not None
            self._socket.sendto(encoded, self.address)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def publish_robot_state(
        self,
        positions: Mapping[str, float],
        *,
        torque_enabled: bool | None,
        connected: bool = True,
        position_unit: str = "degree",
        error: str = "",
    ) -> bool:
        now = time.monotonic()
        clean_positions = {
            str(name): float(value)
            for name, value in positions.items()
        }
        elapsed = (
            now - self._last_position_time
            if self._last_position_time is not None
            else 0.0
        )
        velocities = {
            name: (
                (position - self._last_positions[name]) / elapsed
                if elapsed > 0 and name in self._last_positions
                else 0.0
            )
            for name, position in clean_positions.items()
        }
        self._last_positions = clean_positions
        self._last_position_time = now
        velocity_unit = f"{position_unit}/s" if position_unit else "unit/s"
        return self.publish("robot-state", {
            "connected": bool(connected),
            "torque_enabled": torque_enabled,
            "position_unit": position_unit,
            "velocity_unit": velocity_unit,
            "joints": [
                {
                    "name": name,
                    "position": position,
                    "velocity": velocities[name],
                }
                for name, position in clean_positions.items()
            ],
            "error": error,
        })

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.close()
        except OSError:
            pass
        self._socket = None
