#!/usr/bin/env bash
# Materialize the LRPROOF-patched rns-net from the LOCAL cargo registry cache
# into vendor/rns-net-0.7.2 (gitignored) so the unmodified crates.io crate is
# never committed to the repo. The committed patch file is the only rns-net
# delta the repo carries.
#
# The [patch.crates-io] pin in the workspace Cargo.toml points at the
# materialized dir, so both `cargo build` (Docker deploy) and bazel's
# crate_universe (which reads the same patch) use the fixed crate.
#
# Usage (repo root):
#   rust-client/vendor/fetch-rns-net-patched.sh
# Run once after `git clone`/`cargo clean`; idempotent afterwards.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # rust-client/
CRATE="rns-net-0.7.2"
DEST="$ROOT/vendor/$CRATE"
PATCH="$ROOT/vendor/$CRATE-fix.patch"
CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"

[ -f "$PATCH" ] || { echo "rns-net: patch missing: $PATCH" >&2; exit 1; }

if [ -d "$DEST" ]; then
    echo "rns-net: $DEST already materialized" >&2
    exit 0
fi

SRC="$(ls -d "$CARGO_HOME"/registry/src/index.crates.io-*/$CRATE 2>/dev/null | head -1 || true)"
if [ -z "$SRC" ]; then
    # Cold cache: seed it. Cargo can't resolve past a missing patch path, so
    # fetch with the [patch] temporarily neutralized is not possible; instead
    # try a plain fetch (works when an existing build already cached the tree).
    echo "rns-net: not in cargo cache — running \`cargo fetch\`…" >&2
    ( cd "$ROOT" && CARGO_TARGET_DIR="" cargo fetch ) >/dev/null 2>&1 || true
    SRC="$(ls -d "$CARGO_HOME"/registry/src/index.crates.io-*/$CRATE 2>/dev/null | head -1 || true)"
fi
if [ -z "$SRC" ]; then
    echo "rns-net: ERROR — $CRATE not in $CARGO_HOME/registry/src after fetch." >&2
    echo "          Populate it (any \`cargo build\` of the crate tree does) or" >&2
    echo "          disable the workspace [patch.crates-io] + fetch once." >&2
    exit 1
fi

mkdir -p "$(dirname "$DEST")"
cp -a "$SRC" "$DEST"
( cd "$DEST" && patch -p1 -s < "$PATCH" )
echo "rns-net: materialized + patched -> $DEST" >&2
