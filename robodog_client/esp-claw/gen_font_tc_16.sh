#!/usr/bin/env bash
# Generate the RoboDog RLCD Traditional Chinese LVGL font at build time.
#
# NO generated font is committed (AGENTS.md: third-party generated output is
# fetched/regenerated, never vendored).  The upstream Noto Sans CJK TC OTF is
# sha256-pinned and cached on disk; `lv_font_conv` rasterises the CJK glyph
# ranges into an LVGL C font that the build stages into the fetched esp-claw
# tree (gitignored) next to robodog_ui.c.
#
#   usage: gen_font_tc_16.sh <output.c>
#
# Requirements: Node.js + npx (lv_font_conv 1.5.2), or set LV_FONT_CONV to an
# already-installed lv_font_conv executable.  The generated C font is cached
# under ~/.cache/lmao-fonts, so repeated builds do not re-rasterise 20k glyphs.
set -euo pipefail

OUT="${1:?usage: gen_font_tc_16.sh <output.c>}"

# Noto CJK Sans 2.004 (TraditionalChinese), pinned to the tag's commit.
FONT_COMMIT="523d033d6cb47f4a80c58a35753646f5c3608a78"
FONT_SHA256="dce08bd4fd91aa8aa76ed8fea4b694c2dfb8550f67871e326843212ddbeb88b4"
FONT_URL="https://raw.githubusercontent.com/notofonts/noto-cjk/${FONT_COMMIT}/Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf"
LV_FONT_CONV_VERSION="1.5.2"

# The generated file is keyed by the font hash + converter/ranges/metrics
# revision so a pin bump or a glyph-range change invalidates the cache
# automatically.
GEN_REV="v4-lh20-latin1"
CACHE="${ROBODOG_FONT_CACHE_DIR:-$HOME/.cache/lmao-fonts}"
OTF="$CACHE/NotoSansCJKtc-Regular-${FONT_COMMIT:0:12}.otf"
GEN="$CACHE/robodog_font_tc_16_${FONT_SHA256:0:16}_${GEN_REV}.c"

mkdir -p "$CACHE"
if [ ! -f "$OTF" ]; then
    echo "downloading Noto Sans CJK TC @ ${FONT_COMMIT:0:12} ..."
    curl -fsSL -o "$OTF.tmp" "$FONT_URL"
    echo "$FONT_SHA256  $OTF.tmp" | sha256sum -c - >/dev/null
    mv "$OTF.tmp" "$OTF"
fi
echo "$FONT_SHA256  $OTF" | sha256sum -c - >/dev/null

if [ ! -f "$GEN" ]; then
    if [ -n "${LV_FONT_CONV:-}" ]; then
        LVC="$LV_FONT_CONV"
    elif command -v npx >/dev/null 2>&1; then
        LVC="npx --yes lv_font_conv@${LV_FONT_CONV_VERSION}"
    else
        echo "ERROR: generating the Traditional Chinese font needs Node.js/npx" >&2
        echo "       (lv_font_conv ${LV_FONT_CONV_VERSION}); install Node.js 18+" >&2
        echo "       or set LV_FONT_CONV to an lv_font_conv executable." >&2
        exit 1
    fi
    echo "rasterising Noto Sans CJK TC -> LVGL 16 px/1 bpp font ..."
    # Convert into a CLEAN filename: lv_font_conv derives the generated
    # include guard from the output basename, so a cache-keyed/dotted name
    # yields an invalid `#ifndef ...V3-LH20-LATIN1.C` guard.
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    TMPOUT="$WORK/robodog_font_tc_16.c"
    # Ranges: ASCII + Latin-1 supplement (°C) + general/CJK punctuation +
    # Bopomofo + CJK Unified Ideographs (covers Traditional and Simplified) +
    # compatibility forms.  bpp 1 keeps the bitmap small (~0.7 MB) and crisp
    # on the 1-bpp RLCD; --no-compress matches this LVGL build
    # (LV_USE_FONT_COMPRESSED=n).  Keep in sync with
    # robodog_utf8_glyph_supported().
    $LVC --font "$OTF" --size 16 --bpp 1 --format lvgl --lv-include lvgl.h \
        --no-compress --no-prefilter \
        -r 0x20-0x7E -r 0xA0-0xFF -r 0x2000-0x206F -r 0x3000-0x303F \
        -r 0x3100-0x312F \
        -r 0x3400-0x4DBF -r 0x4E00-0x9FFF -r 0xF900-0xFAFF \
        -r 0xFE30-0xFE4F -r 0xFF00-0xFFEF -o "$TMPOUT"
    # Compact row metrics: the OTF's natural line height is 30 px for a 16 px
    # face, which leaves ~14 px of leading per row and fits only 8 transcript
    # rows on the 300 px RLCD.  20 px/4 px baseline fits every glyph in its
    # cell (max box_h 16 + 4 px descent) and gives 12 rows.  Only the vertical
    # kana repeat marks (U+3031/3032, never in a Chinese reply) exceed it.
    python3 - "$TMPOUT" <<'PY'
import sys
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    src = fh.read()
for old, new in ((".line_height = 30,", ".line_height = 20,"),
                 (".base_line = 9,", ".base_line = 4,")):
    if src.count(old) != 1:
        raise SystemExit(f"font metric patch: expected exactly one {old!r}")
    src = src.replace(old, new)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(src)
PY
    mv "$TMPOUT" "$GEN"
fi

mkdir -p "$(dirname "$OUT")"
cp "$GEN" "$OUT"
echo "Traditional Chinese LVGL font staged at $OUT ($(wc -c < "$OUT") B)"
