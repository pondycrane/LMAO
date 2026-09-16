#!/usr/bin/env bash
# Sprout: vendor the CANONICAL urns + cross-compile to .mpy for the
# ESP32-PICO-D4 small heap, then (optionally) push to the Sprout device.
#
# Single source of truth: the urns port (incl. DtuInterface + lazy-trimmed
# __init__) lives in cardputer_client/lib/urns. The Sprout tree does NOT hold a
# copy — this snapshots it at build time. The raw urns DO NOT fit the PICO-D4
# as source (.py); bytecode is the RAM fix.
# Requires mpy-cross matching the device MicroPython (v1.29.0):
#   pip install --user --break-system-packages mpy-cross==1.29.0.post2
# Usage:
#   tools/build_mpy.sh          # vendor+cross-compile to /tmp/sprout_mpy
#   tools/build_mpy.sh push     # ...and push /lib/urns to the device
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SRC="$ROOT/cardputer_client/lib/urns"
OUT="/tmp/sprout_mpy"
rm -rf "$OUT"; mkdir -p "$OUT"
command -v mpy-cross >/dev/null || { echo "need mpy-cross (see header)"; exit 1; }
[ -d "$SRC" ] || { echo "canonical urns not found: $SRC"; exit 1; }
for f in $(find "$SRC" -name '*.py' | sort); do
  rel="${f#"$SRC"/}"
  mkdir -p "$OUT/lib/urns/$(dirname "$rel")"
  mpy-cross -march=xtensawin "$f" -o "$OUT/lib/urns/${rel%.py}.mpy"
done
echo "compiled $(find "$OUT" -name '*.mpy' | wc -l) urns modules -> $OUT/lib/urns"
if [[ "${1:-}" == "push" ]]; then
  (cd "$OUT" && find lib/urns -name '*.mpy' | while read -r f; do
    mpremote connect /dev/ttyUSB0 fs cp "$f" ":/$f" >/dev/null
  done)
  echo "pushed .mpy tree to device /lib/urns"
fi
