#!/usr/bin/env bash
# Bazel entrypoint to (re)generate the pinned hermes-voice-loop E2E corpus:
#   bazel run //robodog_client:make_corpus [-- --verify]
#
# Runs the SOURCE-TREE copy of `make_corpus.py` so the generated fixtures land
# in the real repo (`robodog_client/voice/corpus/`), not a read-only runfiles
# copy. Uses the ops venv python for consistency with the other voice tools.
set -euo pipefail
if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
    ROOT="$BUILD_WORKSPACE_DIRECTORY"
else
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
VENV="${HERMES_VENV:-$HOME/xiaozhi/.venv/bin/python}"
exec "$VENV" "$ROOT/robodog_client/voice/make_corpus.py" "$@"
