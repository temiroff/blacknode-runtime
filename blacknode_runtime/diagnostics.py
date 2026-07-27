"""Authenticated, read-only diagnostics for services used by deployments."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
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

    nodes = [
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
    robot_topics = sorted({
        line.split(" ", 1)[0]
        for line in topics
        if (
            line.split(" ", 1)[0].startswith(("/leader/", "/follower/"))
            or line.split(" ", 1)[0].endswith(
                ("/joint_states", "/joint_commands", "/robot_control")
            )
        )
    })
    topic_details = [
        {
            "topic": topic,
            **runner(["topic", "info", topic, "--verbose"], 8.0),
        }
        for topic in robot_topics
    ]

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
