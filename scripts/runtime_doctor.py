"""Validate the local Blacknode Runtime installation without starting it."""

from __future__ import annotations

import argparse
import platform
from pathlib import Path

from blacknode_runtime.auth import load_auth_token
from blacknode_runtime.config import RuntimeConfig
from blacknode_runtime.manifest import runtime_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    print("Blacknode Runtime check")
    print("=======================")
    failures = 0
    checks_run = 0

    def check(label: str, fn) -> None:
        nonlocal checks_run, failures
        checks_run += 1
        try:
            value = fn()
            print(f"[OK] {label}: {value}")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {label}: {exc}")

    check("Python", lambda: platform.python_version())
    check("Platform", lambda: f"{platform.system()} {platform.machine()}")
    config_box = {}

    def load_config():
        config_box["value"] = RuntimeConfig.load(args.config)
        return config_box["value"].device_id

    check("Configuration", load_config)
    if "value" in config_box:
        config = config_box["value"]
        check("Pairing token", lambda: f"{len(load_auth_token(Path(config.auth_token_file)))} characters")
        manifest = runtime_manifest(config)
        check(
            "Blacknode",
            lambda: manifest["blacknode"]["version"] or (_ for _ in ()).throw(
                RuntimeError("blacknode core is not installed in this environment")
            ),
        )
        check("Runtime manifest", lambda: f"protocol {manifest['protocol_version']}")
    passed = checks_run - failures
    print()
    print(f"{passed}/{checks_run} required checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
