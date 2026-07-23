"""Check the running runtime service and authenticated manifest."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from blacknode_runtime.auth import load_auth_token


def get_json(url: str, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=0)
    args = parser.parse_args()
    token = load_auth_token(args.token_file)
    deadline = time.monotonic() + max(0, args.wait)
    last_error = ""
    while True:
        try:
            health = get_json(f"{args.url.rstrip('/')}/health")
            manifest = get_json(f"{args.url.rstrip('/')}/manifest", token)
            if health.get("service") != "blacknode-runtime":
                raise RuntimeError("unexpected service identity")
            print("Blacknode Runtime service check")
            print("================================")
            print(f"[OK] Service: {args.url}")
            print(f"[OK] Device: {manifest.get('device_id')}")
            print(f"[OK] Python: {manifest.get('python', {}).get('version')}")
            print(f"[OK] Blacknode: {manifest.get('blacknode', {}).get('version')}")
            print(f"[OK] Runtime: {manifest.get('runtime_version')}")
            print(f"  Features: {', '.join(manifest.get('features') or [])}")
            return 0
        except Exception as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    print("Blacknode Runtime service check")
    print("================================")
    print(f"[FAIL] Service: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
