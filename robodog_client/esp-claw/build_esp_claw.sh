#!/usr/bin/env bash
# Build the RoboDog ESP-Claw (standalone edge_agent) firmware from the PINNED
# UPSTREAM SOURCE (fetch_esp_claw.sh -> espressif/esp-claw, sha256-verified)
# with the RoboDog Phase 1 overlay:
#   - boards/.../setup_device.c LMAO-owned ST7305 panel + RD_SHOT hook,
#     wholesale-copied over the upstream board copy (no fragile patch)
#   - main.c wires robodog_init() (console probes + capture publisher)
#   - components/robodog_phase0/ LMAO-owned component: rdshot / rd_asr /
#     rd_tts / rd_chat probes + the deterministic text-only UI + the Phase 2
#     voice loop (rd_listen -> mic -> ASR -> agent -> TTS -> speaker)
#   - sdkconfig.defaults.robodog (model-endpoint URLs from CLAW_* env + the
#     LVGL text font; the Traditional Chinese face is generated at build time
#     from the pinned Noto Sans CJK TC OTF by gen_font_tc_16.sh, so LAN
#     endpoints stay configurable)
# via the ESP-IDF v5.5.4 Docker image (ESP-Claw's recommended IDF, not the
# v6.0.2 XiaoZhi image).  Nothing vendored: the staged .esp-claw source tree is
# gitignored, only the sha256-pinned commit + LMAO-owned overlay are committed.
#
#   bazel run //robodog_client:build_robodog_claw
#   bash robodog_client/esp-claw/build_esp_claw.sh
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="${1:-$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
else
    APP="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
fi
IDF_IMG="${CLAW_IDF_IMG:-espressif/idf:v5.5.4}"
BOARD="waveshare_ESP32_S3_RLCD_4_2"
BOARD_DIR="boards/waveshare/${BOARD}"

# 1. fetch the pinned source (idempotent)
"$APP/esp-claw/fetch_esp_claw.sh" "$APP"
SRC="$APP/.esp-claw"
EDGE="$SRC/application/edge_agent"

# 2. apply the RoboDog patch (app init; idempotent via its marker) and overlay
#    the LMAO-owned board device file wholesale (no fragile patch).  Touching
#    either invalidates the previous build.
REBUILD_FLAG=""
# 2a. main.c wires robodog_init().
if [ ! -f "$SRC/.robodog_init_patched" ]; then
    echo "applying $APP/esp-claw/patches/0002-init.patch"
    (cd "$SRC" && patch -p1 < "$APP/esp-claw/patches/0002-init.patch")
    touch "$SRC/.robodog_init_patched"
    REBUILD_FLAG=1
fi
# 2c. The shared audio hub runs 24 kHz natively (hermes parity: TTS WAV feeds
#     the codec un-resampled; capture downmixes per-subscriber for 16 kHz
#     VAD/ASR).  Apply over the upstream app_claw config.
if [ ! -f "$SRC/.robodog_audio24k_patched" ]; then
    echo "applying $APP/esp-claw/patches/0003-audio-hub-24k.patch"
    (cd "$SRC" && patch -p1 < "$APP/esp-claw/patches/0003-audio-hub-24k.patch")
    touch "$SRC/.robodog_audio24k_patched"
    REBUILD_FLAG=1
fi
# 2d. Rate-limit the capture ring-overflow warning.  The voice loop keeps its
#     APP capture subscriber OPEN across turns (F3: no per-turn ring teardown),
#     so between turns it is never drained and the upstream warning fires on
#     every 20 ms frame — ~30 KB/s of console output that saturates the
#     USB-JTAG console and blocks the capture task inside ESP_LOGW while a turn
#     is listening (dropped audio -> VAD misses speech -> "back to idle").
if [ ! -f "$SRC/.robodog_drop_log_patched" ]; then
    echo "applying $APP/esp-claw/patches/0004-capture-drop-log-rate-limit.patch"
    (cd "$SRC" && patch -p1 < "$APP/esp-claw/patches/0004-capture-drop-log-rate-limit.patch")
    touch "$SRC/.robodog_drop_log_patched"
    REBUILD_FLAG=1
fi
# 2e. Raise the board's ES7210 mic PGA (upstream leaves it at 0 dB).  The gate's
#     USB test dongle is marginal: with 0 dB PGA the corpus arrives only ~11 dB
#     above the mic noise floor, far under the RT VAD threshold (mean-abs > 300)
#     and unintelligible to ASR (#301).  Patched over the pristine upstream
#     board file - never vendored into the repo.
if [ ! -f "$SRC/.robodog_mic_gain_patched" ]; then
    echo "applying $APP/esp-claw/patches/0005-mic-gain.patch"
    (cd "$SRC" && patch -p1 < "$APP/esp-claw/patches/0005-mic-gain.patch")
    touch "$SRC/.robodog_mic_gain_patched"
    REBUILD_FLAG=1
fi
# 2b. The board's setup_device.c (ST7305 custom panel + the RoboDog RD_SHOT
#     capture hook) is owned by LMAO in the overlay and wholesale-copied over
#     the upstream waveshare board copy each build - drops the fragile
#     0001-rdshot.patch (DESIGN.md: board file + seam move).
BOARD_SRC="$EDGE/boards/waveshare/${BOARD}/setup_device.c"
BOARD_OVL="$APP/esp-claw/overlay/boards/waveshare_ESP32_S3_RLCD_4_2/setup_device.c"
if [ ! -f "$SRC/.robodog_board_overlaid" ] || \
   ! cmp -s "$BOARD_SRC" "$BOARD_OVL"; then
    echo "overlaying LMAO board device: $BOARD_SRC"
    cp -f "$BOARD_OVL" "$BOARD_SRC"
    touch "$SRC/.robodog_board_overlaid"
    REBUILD_FLAG=1
fi

# 3. stage the LMAO-owned overlay component (additive; no upstream files
#    edited): rdshot/rd_asr/rd_tts/rd_chat probes + the deterministic text UI
#    + the Phase 2 voice loop (robodog_voice.c uses the shared audio_hub)
rm -rf "$EDGE/components/robodog_phase0"
cp -r "$APP/esp-claw/overlay/components/robodog_phase0" "$EDGE/components/robodog_phase0"

# 3b. Traditional Chinese RLCD font: rasterised from the sha256-pinned Noto
#     Sans CJK TC OTF by gen_font_tc_16.sh (cached in ~/.cache/lmao-fonts),
#     staged into the component so the UI renders CJK instead of '?'.  The
#     generated C is never committed (AGENTS.md: generated third-party output
#     is fetched/regenerated).
"$APP/esp-claw/gen_font_tc_16.sh" \
    "$EDGE/components/robodog_phase0/src/robodog_font_tc_16.c"

# 4. embed the ASR corpus WAV (single copy of record: voice/corpus/ ->
#    generated C array staged into the fetched tree, never committed)
python3 - "$APP/voice/corpus/capabilities.wav" \
    "$EDGE/components/robodog_phase0/src/robodog_asr_sample.inc" <<'EOF'
import sys
wav = sys.argv[1]
out = sys.argv[2]
data = open(wav, "rb").read()
with open(out, "w") as f:
    f.write("/* Generated by build_esp_claw.sh from robodog_client/voice/corpus/\n")
    f.write(" * capabilities.wav (single corpus copy of record). Never commit. */\n")
    f.write("const uint8_t robodog_asr_sample_wav[] = {\n")
    for i in range(0, len(data), 16):
        f.write("  " + ", ".join(f"0x{b:02x}" for b in data[i:i+16]) + ",\n")
    f.write("};\n")
    f.write("const unsigned int robodog_asr_sample_wav_len = %d;\n" % len(data))
print(f"staged ASR sample {wav} -> {out} ({len(data)} B)")
EOF

# 5. sdkconfig fragment: model endpoints (device-facing, overridable per-build
#    so the committed defaults are not the only path) + LVGL text-UI font.
#    Defaults are the LAN IP of spark-5307 (Whisper :8001 / Qwen3-TTS :8004),
#    which sits on the BOARD's own 192.168.50.0/24 subnet — the device reaches
#    it directly, no host socat forward.  An IP is used rather than the host
#    name because the board's resolver does not know the LAN names.
#
#    TTS_VOICE is EMPTY by default (per-language timbres): English replies
#    use CONFIG_ROBODOG_TTS_VOICE_EN (aiden), Chinese replies use
#    CONFIG_ROBODOG_TTS_VOICE_ZH (dylan, the Beijing-native timbre).
#    A Chinese-native timbre reading English carries a Chinese accent that
#    `instruct` cannot remove, so the English branch must not use dylan.
#    Taiwan has several Mandarin varieties, so the Chinese branch names the
#    Beijing accent explicitly; a generic "standard Mandarin" still drifted.
#    CLAW_TTS_VOICE=<timbre> pins ONE voice for every reply (overrides the
#    per-language pair).  Every request sends a fixed seed (CLAW_TTS_SEED,
#    default 8) and the Chinese Beijing instruct (CLAW_TTS_INSTRUCT_ZH).
cat > "$EDGE/sdkconfig.defaults.robodog" <<EOF
# RoboDog Phase 1 model endpoints (generated by build_esp_claw.sh from CLAW_*
# env; defaults match the user's LAN deployment documented in DESIGN.md).
# Phase 3 omni realtime session (vLLM-Omni OpenAI-Realtime).  This is the
# voice path: one WebSocket session streams mic in and transcript+PCM out,
# replacing the three serial HTTP hops below (measured 24-32 s from
# end-of-speech to first audio).  CLAW_RT_URL overrides it per build.
CONFIG_ROBODOG_RT_URL="${CLAW_RT_URL:-ws://192.168.50.27:8099/v1/realtime}"
CONFIG_ROBODOG_RT_MODEL="${CLAW_RT_MODEL:-openbmb/MiniCPM-o-4_5}"
CONFIG_ROBODOG_RT_TURN_MODE=${CLAW_RT_TURN_MODE:-0}
# Phase 2 fallback endpoints (still used by rd_asr/rd_tts probes and by the
# AGENT-shaped turn, where claw_core answers and Qwen3-TTS speaks).
CONFIG_ROBODOG_ASR_URL="${CLAW_ASR_URL:-http://192.168.50.27:8001/v1/audio/transcriptions}"
CONFIG_ROBODOG_ASR_MODEL="${CLAW_ASR_MODEL:-whisper-1}"
CONFIG_ROBODOG_TTS_URL="${CLAW_TTS_URL:-http://192.168.50.27:8004/v1/audio/speech}"
CONFIG_ROBODOG_TTS_MODEL="${CLAW_TTS_MODEL:-qwen3-tts}"
CONFIG_ROBODOG_TTS_VOICE="${CLAW_TTS_VOICE-}"
CONFIG_ROBODOG_TTS_VOICE_EN="${CLAW_TTS_VOICE_EN:-aiden}"
CONFIG_ROBODOG_TTS_VOICE_ZH="${CLAW_TTS_VOICE_ZH:-dylan}"
CONFIG_ROBODOG_TTS_SEED=${CLAW_TTS_SEED:-8}
CONFIG_ROBODOG_TTS_INSTRUCT_ZH="${CLAW_TTS_INSTRUCT_ZH:-請用標準北京腔的普通話說話，發音清晰，語調自然，語速適中，不要帶台灣腔、粵語或南方口音。}"
CONFIG_ROBODOG_TTS_INSTRUCT_EN="${CLAW_TTS_INSTRUCT_EN-}"

# Phase 1 text-only UI: ~16 px monochrome font.  The body/status labels use
# the generated Noto Sans CJK TC 16 px 1 bpp font (gen_font_tc_16.sh, staged
# into the component) so Traditional Chinese renders as real glyphs;
# Montserrat-16 stays enabled as LVGL's default/fallback face.
CONFIG_LV_FONT_MONTSERRAT_16=y

# Keep the RLCD owned by OUR Phase-1 text UI.  The upstream Waveshare board's
# sdkconfig.defaults.board sets CONFIG_ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT=y (and
# so APP_CLAW_SYSTEM_UI_ENABLE=y), which compiles IN esp-claw's own
# display_service / system_ui: that renders the black softAP+clock home screen
# (with the floating animation) on top of our panel.  Force both back off here
# (this fragment merges LAST in SDKCONFIG_DEFAULTS) so esp-claw's display stack
# is gated out regardless of what the board-manager regen decides, and
# robodog_ui is the sole RLCD owner — matching DESIGN Phase 1.
CONFIG_ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT=n
CONFIG_APP_CLAW_SYSTEM_UI_ENABLE=n
EOF
# Echo the realtime-lane knobs into the build log: the bench builds with a
# payload env, and a wrong/missing turn mode silently changes which voice path
# the flashed image runs (the gate then asserts the wrong lane).
printf 'realtime lane: url=%s model=%s turn_mode=%s\n' \
    "${CLAW_RT_URL:-ws://192.168.50.27:8099/v1/realtime}" \
    "${CLAW_RT_MODEL:-openbmb/MiniCPM-o-4_5}" \
    "${CLAW_RT_TURN_MODE:-0}"

# 5b. Drop any stale sdkconfig before configuring.  `idf.py` keeps sdkconfig
#     across builds and its values WIN over SDKCONFIG_DEFAULTS, so a knob whose
#     default changed (e.g. ROBODOG_VOICE_MIN_SPEECH_MS 300 -> 100) silently
#     stayed at the old value while the committed Kconfig said otherwise — the
#     flashed firmware then disagreed with the source and the E2E measured the
#     stale tuning.  Removing it makes every build configure from the committed
#     defaults (sdkconfig.defaults + board + robodog), i.e. the build is a pure
#     function of the committed sources.
rm -f "$EDGE/sdkconfig"

# 6. build in the ESP-IDF Docker image (bmgr selects the board + its target +
#    partition table automatically, no set-target; explicit SDKCONFIG_DEFAULTS
#    mirrors the XiaoZhi build precedent deterministically)
# The IDF image's bundled idf-component-manager (2.x) records root local-source
# requirements by their RAW `path:` string, so a sibling local component whose
# manifest references claw_core with a relative `../x`-style path hashes
# differently from claw_core's self-root absolute path and the solver aborts
# ("Already defined root local requirement").  Rewriting every relative `path:`
# to its absolute location (rewrite_component_paths.py, run inside the
# container where the tree is mounted at /workspace) unifies the hash keys.
# esp-bmgr-assist provides the `idf.py bmgr` extension.
exec docker run --rm \
    -v "$SRC:/workspace" -w /workspace \
    -v "$APP/esp-claw/rewrite_component_paths.py:/rewrite_component_paths.py" \
    "$IDF_IMG" bash -c "
    python -m pip install -q esp-bmgr-assist
    if [ ! -f /workspace/.robodog_paths_rewritten ]; then
        python3 /rewrite_component_paths.py /workspace
        touch /workspace/.robodog_paths_rewritten
    fi
    cd application/edge_agent
    idf.py bmgr -c ./boards -b $BOARD
    idf.py -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;${BOARD_DIR}/sdkconfig.defaults.board;sdkconfig.defaults.robodog' build
"
