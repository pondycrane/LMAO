#!/usr/bin/env bash
# T0 DECISION GATE shim — hosts the Rust↔Python RNS Link+Resource interop gate
# from Bazel. Out-of-band (no rules_rust in this repo), mirroring the
# native-client ESP-IDF/`interop` convention: a manual sh_binary over a
# filegroup of the whole Rust tree.
#
#   bazel run //rust-client:run_t0_gate -- --python /path/to/python
#   (needs a Python with RNS; default: LMAO_PYTHON env or `python3`)
set -euo pipefail

# bazel run executes this from the runfiles dir; BUILD_WORKSPACE_DIRECTORY
# points at the real workspace root so we find the live Cargo workspace.
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi

# python interpreter override:  --python <path>  |  --python=<path>  |  $LMAO_PYTHON
PY="${LMAO_PYTHON:-}"
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  case "${args[$i]}" in
    --) : ;;
    --python) PY="${args[$((i + 1))]}" ; i=$((i + 1)) ;;
    --python=*) PY="${args[$i]#--python=}" ;;
    *) ;;
  esac
  i=$((i + 1))
done
[ -n "$PY" ] || PY="python3"

if ! LMAO_PYTHON="$PY" cargo run -q -p lmao-t0-interop; then
  echo "[t0-gate] FAIL — see interop log above" >&2
  exit 1
fi
