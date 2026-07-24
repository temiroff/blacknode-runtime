"""Run the authenticated Blacknode Runtime service."""

from __future__ import annotations

import argparse
from pathlib import Path

from blacknode_runtime.auth import load_auth_token
from blacknode_runtime.config import RuntimeConfig
from blacknode_runtime.deployments import DeploymentStore
from blacknode_runtime.package_manager import PackageManager
from blacknode_runtime.server import serve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    config = RuntimeConfig.load(args.config)
    token = load_auth_token(Path(config.auth_token_file))
    store = DeploymentStore(Path(config.state_dir) / "deployments")
    package_manager = PackageManager(Path(__file__).resolve().parents[1] / "packages")
    serve(
        config,
        store,
        token,
        package_manager=package_manager,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
