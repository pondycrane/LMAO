#!/usr/bin/env bash
# T7 HOST E2E shim — the composed leaf pipeline (design §5 data-flow, minus the
# RF leg). Out-of-band (no rules_rust): a manual sh_binary over the Rust tree,
# mirroring native-client. Needs protoc for lma-wire (export PROTOC).
#
#   bazel run //rust-client:run_leaf_e2e
set -euo pipefail

if [ -n "${PROTOC:-}" ] || command -v protoc >/dev/null 2>&1; then
  :
else
  echo "error: protoc not found (lma-wire build). Set PROTOC=/path/to/protoc." >&2
  exit 1
fi

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client"
else
  cd "$(dirname "$0")"
fi

if ! command -v cargo >/dev/null 2>&1; then
  for c in "$HOME/.cargo/bin/cargo" /usr/local/cargo/bin/cargo; do
    [ -x "$c" ] && PATH="$PATH:$(dirname "$c")" && break
  done
fi

if ! cargo run -q -p lmao-leaf-e2e; then
  echo "[t7-e2e] FAIL" >&2
  exit 1
fi
