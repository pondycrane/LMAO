#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the leaf-rns crate (T4 link-window tests).
# Out-of-band (no rules_rust); manual so never pulled into `bazel test //tests:...`.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi
cargo test -p leaf-rns "$@"
