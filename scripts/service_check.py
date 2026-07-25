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


def print_deployments(payload: dict) -> None:
    deployments = [
        item
        for item in (payload.get("deployments") or [])
        if isinstance(item, dict)
    ]
    running = sum(
        1 for item in deployments
        if str(item.get("state") or "") == "running"
    )
    print()
    print("Deployments")
    print("-----------")
    print(f"{len(deployments)} total · {running} running")
    if not deployments:
        print("No deployments are staged on this runtime.")
        return
    for item in deployments:
        state = str(item.get("state") or "unknown").upper()
        name = str(item.get("name") or item.get("id") or "Deployment")
        print(f"[{state}] {name}")
        print(f"  ID: {item.get('id') or '—'}")
        print(f"  Target robot: {item.get('target_device_id') or 'not recorded'}")
        if item.get("project_id"):
            print(f"  Project: {item['project_id']}")
            print(f"  Workflow: {item.get('workflow_slug') or 'not recorded'}")
        else:
            print("  Project: unassigned")
        print(f"  PID: {item.get('pid') or '—'}")
        print(f"  Revision: {item.get('active_revision') or item.get('staged_revision') or '—'}")
        print(f"  Updated: {item.get('updated_at') or '—'}")
        if item.get("error"):
            print(f"  Error: {item['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=0)
    parser.add_argument(
        "--deployments",
        action="store_true",
        help="also list staged and running deployments",
    )
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
            if args.deployments:
                print_deployments(
                    get_json(f"{args.url.rstrip('/')}/deployments", token)
                )
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
