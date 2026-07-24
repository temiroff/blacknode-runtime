"""Render the systemd unit for Blacknode Runtime."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("systemd values cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def working_directory(value: str) -> str:
    if any(character.isspace() for character in value) or any(character in value for character in '"\\\r\n'):
        raise ValueError("repository path cannot contain spaces, quotes, or backslashes")
    return value.replace("%", "%%")


def render_unit(*, repo: Path, user: str, host: str, port: int, config: Path, state_dir: Path) -> str:
    repo = repo.resolve()
    config = config.resolve()
    state_dir = state_dir.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
        raise ValueError("service user contains unsupported characters")
    if not 1 <= port <= 65535:
        raise ValueError("service port must be from 1 to 65535")
    python = repo / ".venv" / "bin" / "python"
    doctor = repo / "scripts" / "runtime_doctor.py"
    service = repo / "scripts" / "runtime_service.py"
    package_path = repo / "packages"
    return "\n".join([
        "[Unit]",
        "Description=Blacknode Runtime Service",
        "Wants=network-online.target docker.service",
        "After=network-online.target docker.service blacknode-hardware.service",
        "StartLimitIntervalSec=60",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}",
        f"WorkingDirectory={working_directory(str(repo))}",
        'Environment="PYTHONUNBUFFERED=1"',
        f"Environment={quote(f'BLACKNODE_PACKAGE_PATH={package_path}')}",
        f"ExecStartPre={quote(str(python))} {quote(str(doctor))} --config {quote(str(config))}",
        (
            f"ExecStart={quote(str(python))} {quote(str(service))} "
            f"--host {quote(host)} --port {port} --config {quote(str(config))}"
        ),
        "Restart=on-failure",
        "RestartSec=2s",
        "TimeoutStopSec=10s",
        "KillMode=control-group",
        "KillSignal=SIGINT",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
        f"ReadWritePaths={quote(str(state_dir))}",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    print(render_unit(
        repo=args.repo,
        user=args.user,
        host=args.host,
        port=args.port,
        config=args.config,
        state_dir=args.state_dir,
    ), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
