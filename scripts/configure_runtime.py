"""Create or inspect the local Blacknode Runtime configuration."""

from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

from blacknode_runtime.config import RuntimeConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--device-id")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--hardware-url")
    parser.add_argument("--blacknode-root", type=Path)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    existing = {}
    if args.config.is_file():
        existing = json.loads(args.config.read_text(encoding="utf-8"))
    if args.show:
        config = RuntimeConfig.load(args.config)
    else:
        token_file = args.token_file or (
            Path(existing["auth_token_file"]) if existing.get("auth_token_file") else None
        )
        if token_file is None:
            parser.error("--token-file is required for the first configuration")
        state_dir = args.state_dir or (
            Path(existing["state_dir"]) if existing.get("state_dir")
            else args.config.parent / "state"
        )
        config = RuntimeConfig.from_values(
            device_id=args.device_id or existing.get("device_id") or socket.gethostname(),
            auth_token_file=str(token_file),
            state_dir=str(state_dir),
            hardware_url=args.hardware_url or existing.get("hardware_url") or "http://127.0.0.1:8765",
            blacknode_root=(
                str(args.blacknode_root)
                if args.blacknode_root is not None
                else str(existing.get("blacknode_root") or "")
            ),
        )
        config.save(args.config)

    print("Blacknode Runtime configuration")
    print("================================")
    print(f"File: {args.config.resolve()}")
    print(f"Device ID: {config.device_id}")
    print(f"Hardware URL: {config.hardware_url}")
    print(f"Token file: {config.auth_token_file}")
    print(f"State directory: {config.state_dir}")
    print(f"Blacknode root: {config.blacknode_root or 'installed package'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
