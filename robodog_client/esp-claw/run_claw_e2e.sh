#!/usr/bin/env bash
# Bazel entrypoint for the RoboDog ESP-Claw voice-loop E2E:
#   bazel run //robodog_client:claw_e2e [-- --corpus capabilities --artifacts DIR]
#
# Runs the SOURCE-TREE copy of `robodog_claw_e2e.py`. The script imports
# `capture` (RD_SHOT decode + vision readback) plus pyserial/Pillow — the
# @lmao_pip Bazel set does not carry the vision/audio extras, so this is a
# host-venv tool (sh_binary) like the hermes E2E.  CLAW_E2E_PYTHON overrides
# the interpreter (default: the system python3, which has serial+Pillow on the
# rig; set it to ~/xiaozhi/.venv/bin/python if the extras live there).
set -euo pipefail
if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
    ROOT="$BUILD_WORKSPACE_DIRECTORY"
else
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
PY="${CLAW_E2E_PYTHON:-python3}"
exec "$PY" "$ROOT/robodog_client/esp-claw/robodog_claw_e2e.py" "$@"
