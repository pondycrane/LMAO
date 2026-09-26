#!/usr/bin/env bash
# Bazel wrapper → `cargo build` for the whole rust-client workspace.
# Out-of-band (repo has no rules_rust); keeps every new Rust file Bazel-visible.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi
cargo build --workspace "$@"
