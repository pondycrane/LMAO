#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the lma-lxmf crate (T5 LXMF control path).
# Depends on lma-wire, so also needs PROTOC (export PROTOC=/path/to/protoc).
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
cargo test -p lma-lxmf "$@"
