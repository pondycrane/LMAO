#!/usr/bin/env bash
# RoboDog ESP-Claw multi-session STRESS gate runner (DESIGN.md R7 soak).
# Runs N consecutive voice sessions, each asking a different corpus question,
# and asserts every session reaches ASR/reply/TTS (no turn collapses back to
# idle) with no board reset/panic.  Host python tool; never writes
# hardware-e2e.md / .gate-head.
#
#   bazel run //robodog_client:claw_stress [-- --artifacts DIR --runs 5]
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
else
    APP="$(cd "$(dirname "$0")/.." && pwd)"
fi
exec python3 "$APP/esp-claw/robodog_claw_stress.py" "$@"
