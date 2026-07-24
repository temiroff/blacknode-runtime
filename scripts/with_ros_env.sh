#!/usr/bin/env bash
set -euo pipefail

ros_setup=""
if [[ -n "${ROS_DISTRO:-}" && -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
  ros_setup="/opt/ros/${ROS_DISTRO}/setup.bash"
elif [[ -f "/opt/ros/jazzy/setup.bash" ]]; then
  ros_setup="/opt/ros/jazzy/setup.bash"
else
  for candidate in /opt/ros/*/setup.bash; do
    if [[ -f "$candidate" ]]; then
      ros_setup="$candidate"
    fi
  done
fi

if [[ -n "$ros_setup" ]]; then
  # ROS setup scripts may reference variables that are initially unset.
  set +u
  # shellcheck disable=SC1090
  source "$ros_setup"
  set -u
fi

exec "$@"
