"""Git-ignored runtime service configuration."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    device_id: str
    auth_token_file: str
    state_dir: str
    hardware_url: str = "http://127.0.0.1:8765"
    blacknode_root: str = ""

    @classmethod
    def load(cls, path: Path) -> "RuntimeConfig":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read runtime configuration: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("runtime configuration must be a JSON object")
        return cls.from_values(**payload)

    @classmethod
    def from_values(
        cls,
        *,
        device_id: str = "",
        auth_token_file: str,
        state_dir: str,
        hardware_url: str = "http://127.0.0.1:8765",
        blacknode_root: str = "",
        **_extra: Any,
    ) -> "RuntimeConfig":
        token_path = Path(auth_token_file).expanduser().resolve()
        state_path = Path(state_dir).expanduser().resolve()
        root_path = Path(blacknode_root).expanduser().resolve() if blacknode_root else None
        clean_device_id = str(device_id or socket.gethostname()).strip()
        if not clean_device_id:
            raise ValueError("device_id is required")
        if not token_path.is_file():
            raise ValueError(f"pairing token file does not exist: {token_path}")
        if not str(hardware_url).startswith(("http://", "https://")):
            raise ValueError("hardware_url must start with http:// or https://")
        return cls(
            device_id=clean_device_id,
            auth_token_file=str(token_path),
            state_dir=str(state_path),
            hardware_url=str(hardware_url).rstrip("/"),
            blacknode_root=str(root_path) if root_path else "",
        )

    def save(self, path: Path) -> Path:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(self), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
