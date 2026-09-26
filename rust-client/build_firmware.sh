#!/usr/bin/env bash
# Build the T1 no_std firmware (xtensa-esp32s3-none-elf) via the esp nightly
# toolchain (out-of-band cargo — no rules_rust). Bazel: `bazel run //rust-client:build_firmware`.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client/firmware"
else
  cd "$(dirname "$0")/firmware"
fi
cargo build --release "$@"
