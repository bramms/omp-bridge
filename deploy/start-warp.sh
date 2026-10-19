#!/bin/bash
set -euo pipefail
bridge_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_args=()
if [[ "$(uname -s)" == Darwin ]]; then
    if ! colima status >/dev/null 2>&1; then
        colima start --activate=false --save-config=false
    fi
    docker_args=(--context colima)
fi
docker "${docker_args[@]}" compose -f "$bridge_root/deploy/warp/compose.yaml" up -d --no-build
exec "${OMP_FREE_PYTHON:-python3}" "$bridge_root/bridge.py" serve "$@"
