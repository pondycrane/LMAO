#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the lma-wire crate (T5 §6b prost↔Python wire).
# prost-build needs protoc: export PROTOC=/path/to/protoc first (see T5-GATE.md).
# Out-of-band (no rules_rust); manual so never pulled into `bazel test //tests:...`.
set -euo pipefail
if [ -n "${PROTOC:-}" ] || command -v protoc >/dev/null 2>&1; then
  :
else
  echo "error: protoc not found. Set PROTOC=/path/to/protoc (see T5-GATE.md)." >&2
  exit 1
fi
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi
cargo test -p lma-wire "$@"
