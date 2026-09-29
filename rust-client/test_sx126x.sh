#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the sx126x driver core (RF-leg).
# Out-of-band (no rules_rust); manual so never pulled into `bazel test //tests:...`.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"; else cd "$(dirname "$0")"; fi
cargo test -p sx126x "$@"
