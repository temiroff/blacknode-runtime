"""Authenticated, read-only diagnostics for services used by deployments."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable


CommandRunner = Callable[[list[str], float], dict[str, Any]]
_CONTROL_TOPIC_RE = re.compile(
    r"/blacknode/leader_follower/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*"
)


def is_ros2_armed_control_topic(topic: str) -> bool:
    return _CONTROL_TOPIC_RE.fullmatch(str(topic or "").strip()) is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_ros2(args: list[str], timeout: float = 8.0) -> dict[str, Any]:
    command = ["ros2", *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "error": f"command timed out after {timeout:g}s",
        }
    except OSError as exc:
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
        }
    return {
        "command": command,
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "error": "" if result.returncode == 0 else result.stderr.strip(),
    }


def ros2_diagnostics(
    runner: CommandRunner = _run_ros2,
) -> dict[str, Any]:
    """Inspect ROS 2 discovery and robot topics without publishing commands."""
    if shutil.which("ros2") is None and runner is _run_ros2:
        return {
            "ok": False,
            "available": False,
            "checked_at": _now(),
            "summary": "The ros2 command is unavailable in the Runtime service environment.",
            "nodes": [],
            "topics": [],
            "services": [],
            "topic_details": [],
            "warnings": [],
        }

    node_result = runner(["node", "list"], 8.0)
    topic_result = runner(["topic", "list", "-t"], 8.0)
    service_result = runner(["service", "list"], 8.0)

    discovered_nodes = [
        line.strip()
        for line in str(node_result.get("stdout") or "").splitlines()
        if line.strip()
    ]
    topics = [
        line.strip()
        for line in str(topic_result.get("stdout") or "").splitlines()
        if line.strip()
    ]
    services = [
        line.strip()
        for line in str(service_result.get("stdout") or "").splitlines()
        if line.strip()
    ]
    topic_names = sorted({
        line.split(" ", 1)[0]
        for line in topics
        if line.split(" ", 1)[0]
    })

    def inspect_topic(topic: str) -> dict[str, Any]:
        return {
            "topic": topic,
            **runner(["topic", "info", topic, "--verbose"], 8.0),
        }

    # These commands only inspect discovery metadata. Run a small bounded set
    # concurrently so a full graph snapshot remains responsive as topic count
    # grows, while preserving deterministic topic order in the response.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(topic_names)))) as executor:
        topic_details = list(executor.map(inspect_topic, topic_names))

    endpoint_nodes: set[str] = set()
    endpoint_pattern = re.compile(
        r"Node name:\s*([^\r\n]+)\r?\nNode namespace:\s*([^\r\n]+)",
        re.IGNORECASE,
    )
    for detail in topic_details:
        for node_name, namespace in endpoint_pattern.findall(
            str(detail.get("stdout") or "")
        ):
            clean_namespace = namespace.strip().rstrip("/")
            endpoint_nodes.add(
                f"{clean_namespace}/{node_name.strip()}"
                if clean_namespace
                else f"/{node_name.strip()}"
            )
    service_counts = Counter(
        node
        for node in discovered_nodes
        for service in services
        if service == node or service.startswith(f"{node}/")
    )
    # The ROS CLI daemon can retain destroyed helper names. A live node should
    # own a discovered topic endpoint or more than one node-scoped service.
    # Only filter when verbose topic inspection returned endpoint identities;
    # older ROS releases that omit them keep the unfiltered discovery result.
    nodes = (
        [
            node
            for node in discovered_nodes
            if node in endpoint_nodes or service_counts[node] > 1
        ]
        if endpoint_nodes
        else discovered_nodes
    )
    stale_nodes = sorted(set(discovered_nodes) - set(nodes))

    warnings: list[str] = []
    duplicates = sorted(name for name, count in Counter(nodes).items() if count > 1)
    if duplicates:
        warnings.append(
            "Duplicate ROS 2 node names: " + ", ".join(duplicates)
        )
    command_failures = [
        str(item.get("error") or "command failed")
        for item in (node_result, topic_result, service_result, *topic_details)
        if not item.get("ok")
    ]
    if command_failures:
        warnings.append("Some ROS 2 checks failed: " + "; ".join(command_failures[:3]))

    ok = bool(
        node_result.get("ok")
        and topic_result.get("ok")
        and service_result.get("ok")
    )
    return {
        "ok": ok,
        "available": True,
        "checked_at": _now(),
        "summary": (
            f"Found {len(nodes)} nodes, {len(topics)} topics, and "
            f"{len(services)} services."
            if ok
            else "ROS 2 diagnostics completed with errors."
        ),
        "nodes": nodes,
        "stale_nodes": stale_nodes,
        "topics": topics,
        "services": services,
        "topic_details": topic_details,
        "warnings": warnings,
        "commands": {
            "nodes": node_result,
            "topics": topic_result,
            "services": service_result,
        },
    }


def inspect_ros2_interfaces(
    expectations: list[dict[str, Any]],
    runner: CommandRunner = _run_ros2,
) -> dict[str, Any]:
    """Check a small declared ROS 2 interface group without scanning the graph."""
    checked_at = _now()
    topic_result = runner(["topic", "list", "-t"], 8.0)
    discovered: dict[str, str] = {}
    for line in str(topic_result.get("stdout") or "").splitlines():
        match = re.fullmatch(r"\s*(/\S+)\s+\[([^\]]+)\]\s*", line)
        if match:
            discovered[match.group(1)] = match.group(2)

    interfaces: list[dict[str, Any]] = []
    for raw in expectations:
        topic = str(raw.get("topic") or "").strip()
        expected_type = str(raw.get("type") or "").strip()
        required = bool(raw.get("required", True))
        direction = str(raw.get("direction") or "publisher").strip().lower()
        actual_type = discovered.get(topic, "")
        present = bool(actual_type)
        detail = (
            runner(["topic", "info", topic, "--verbose"], 8.0)
            if present
            else {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "error": "topic is not present",
            }
        )
        output = str(detail.get("stdout") or "")
        publisher_match = re.search(r"Publisher count:\s*(\d+)", output)
        subscriber_match = re.search(r"Subscription count:\s*(\d+)", output)
        publishers = int(publisher_match.group(1)) if publisher_match else None
        subscribers = int(subscriber_match.group(1)) if subscriber_match else None
        type_matches = not expected_type or actual_type == expected_type
        endpoint_ready = (
            publishers is None or publishers > 0
            if direction == "publisher"
            else subscribers is None or subscribers > 0
        )
        ready = bool(present and type_matches and detail.get("ok") and endpoint_ready)
        interfaces.append({
            "topic": topic,
            "type": actual_type,
            "expected_type": expected_type,
            "required": required,
            "direction": direction,
            "present": present,
            "publishers": publishers,
            "subscribers": subscribers,
            "ready": ready,
            "error": (
                ""
                if ready
                else (
                    f"expected {expected_type}, found {actual_type}"
                    if present and not type_matches
                    else str(detail.get("error") or "required endpoint is not ready")
                )
            ),
        })

    missing = [
        item["topic"]
        for item in interfaces
        if item["required"] and not item["ready"]
    ]
    return {
        "ok": bool(topic_result.get("ok")) and not missing,
        "available": bool(topic_result.get("ok")),
        "checked_at": checked_at,
        "interfaces": interfaces,
        "missing": missing,
        "summary": (
            f"{len(interfaces) - len(missing)}/{len(interfaces)} declared interfaces ready."
            if topic_result.get("ok")
            else str(topic_result.get("error") or "ROS 2 topic discovery failed")
        ),
    }


def publish_ros2_armed_control(
    topic: str,
    armed: bool,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Publish one vetted arm-state message to a deployment-owned topic."""
    clean_topic = str(topic or "").strip()
    if not is_ros2_armed_control_topic(clean_topic):
        return {
            "ok": False,
            "armed": armed,
            "topic": clean_topic,
            "error": "deployment arm control topic is invalid",
        }
    payload = json.dumps({"data": json.dumps({"armed": bool(armed)})})
    result = _run_ros2(
        [
            "topic",
            "pub",
            "--once",
            clean_topic,
            "std_msgs/msg/String",
            payload,
        ],
        timeout,
    )
    return {
        **result,
        "armed": bool(armed),
        "topic": clean_topic,
    }
