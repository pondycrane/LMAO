#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the leaf-lxmf crate (L1 LXMF opportunistic
# wire format + inbound mirror decode). no_std, no prost — no PROTOC needed.
# Out-of-band (no rules_rust); manual so never pulled into `bazel test //tests:...`.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi
cargo test -p leaf-lxmf "$@"
