#!/usr/bin/env bash
# Bazel wrapper → `cargo test` for the lma-framing crate (T8 successor framing
# on Resource). Needs protoc (depends on lma-wire). Out-of-band; manual.
set -euo pipefail
if [ -n "${PROTOC:-}" ] || command -v protoc >/dev/null 2>&1; then :; else
  echo "error: protoc not found (lma-wire). Set PROTOC=/path/to/protoc." >&2; exit 1; fi
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"; else cd "$(dirname "$0")"; fi
cargo test -p lma-framing "$@"
