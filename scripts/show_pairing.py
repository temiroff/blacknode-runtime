"""Display the private runtime credential for an explicit editor pairing."""

from __future__ import annotations

import argparse
from pathlib import Path

from blacknode_runtime.auth import load_auth_token, token_fingerprint
from blacknode_runtime.config import RuntimeConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--url", default="http://DEVICE_IP:8766")
    args = parser.parse_args()

    config = RuntimeConfig.load(args.config)
    token_path = Path(config.auth_token_file)
    token = load_auth_token(token_path)
    print("Blacknode Runtime pairing")
    print("=========================")
    print(f"Address: {args.url}")
    print(f"Token file: {token_path}")
    print(f"Fingerprint: {token_fingerprint(token)}")
    print(f"Runtime token: {token}")
    print()
    print("Keep this token private. Paste it into Devices > Runtime token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
