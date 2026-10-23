#!/usr/bin/env bash
# Bazel entrypoint for the RoboDog hermes voice-loop E2E (issue #267):
#   bazel run //robodog_client:hermes_voice_e2e [-- --corpus count --artifacts DIR]
#
# Runs the SOURCE-TREE copy of `hermes_voice_e2e.py` under the ops bridge venv
# (~/xiaozhi/.venv). The script imports `voice_bridge`/`capture`, whose deps
# (faster_whisper, fastapi, numpy, opuslib, soundfile ...) live in that venv,
# not in the @lmao_pip bazel set — so this is a host-venv tool (sh_binary),
# exactly like the firmware flash/build targets.
#
# Bazel sets BUILD_WORKSPACE_DIRECTORY to the real repo on `bazel run`; fall
# back to the script's own location so the wrapper also works when run by hand.
set -euo pipefail
if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
    ROOT="$BUILD_WORKSPACE_DIRECTORY"
else
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
VENV="${HERMES_VENV:-$HOME/xiaozhi/.venv/bin/python}"
exec "$VENV" "$ROOT/robodog_client/voice/hermes_voice_e2e.py" "$@"
