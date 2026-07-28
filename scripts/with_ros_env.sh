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

# Extension packages may ship isolated ROS 2 workspaces. Source every built
# package workspace after the base ROS distribution so managed services and
# deployments can resolve their executables. BLACKNODE_PACKAGE_PATH follows
# the host path separator convention; Runtime installations use ':' on Linux.
if [[ -n "${BLACKNODE_PACKAGE_PATH:-}" ]]; then
  IFS=':' read -r -a package_roots <<< "$BLACKNODE_PACKAGE_PATH"
  for package_root in "${package_roots[@]}"; do
    if [[ ! -d "$package_root" ]]; then
      continue
    fi
    while IFS= read -r -d '' workspace_setup; do
      set +u
      # shellcheck disable=SC1090
      source "$workspace_setup"
      set -u
    done < <(
      find "$package_root" -type f \
        -path '*/ros2_ws/install/setup.bash' -print0 | sort -z
    )
  done
fi

exec "$@"
