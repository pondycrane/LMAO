#!/usr/bin/env bash
# Flash robodog_client to the Waveshare ESP32-S3 RLCD, button-free.
#
# IMPORTANT: this runs esptool on the HOST, not in Docker.  Docker's ttyACM
# passthrough drops the RTS/DTR modem-control ioctls (EPROTO on open), which
# breaks esptool's reset into download mode — so the button-free
# `--before default_reset` auto-reset only works when esptool runs directly on
# the host.  Requires host `python3 -m esptool`.
#
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM0]
#   bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
#
# `--kid-name <kid>` (non-empty) writes the board's NVS `kid_name` at flash
# time (NVS partition at 0x9000, 24 KB via IDF nvs_partition_gen.py in the same
# Docker image that builds): the board becomes a kid-dedicated panel.  Writing
# NVS WIPES the whole 24 KB partition (event cache + any NVS wifi/server keys)
# — desired for re-assignment delta-safety, harmless here because network
# config comes from local_secrets.h defines.  An explicit `--kid-name ""`
# writes an EMPTY NVS image (wipes the partition, no kid_name key) -> clears a
# previously-assigned board back to family mode.  No `--kid-name` flag at all
# leaves NVS untouched (fresh board family via the pinned RD_KID_NAME_DEFAULT).
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
PORT="${FLASH_PORT:-/dev/ttyACM0}"
BAUD="${FLASH_BAUD:-460800}"
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"
KID=""
KID_FLAG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            PORT="${2:-$PORT}"
            shift 2 || true
            ;;
        --kid-name)
            KID="${2:-}"
            KID_FLAG=1
            shift 2 || true
            ;;
        *)
            echo "unknown argument: $1 (expected --port <port>, --kid-name <kid>)" >&2
            exit 2
            ;;
    esac
done
if ! python3 -c "import esptool" >/dev/null 2>&1; then
    echo "host esptool is required (button-free reset can't run in Docker):" >&2
    echo "  python3 -m pip install --target=\$HOME/.local/lib python esptool" >&2
    exit 1
fi

NVS_WORK=""
cleanup() {
    if [ -n "$NVS_WORK" ] && [ -d "$NVS_WORK" ]; then
        rm -rf "$NVS_WORK"
    fi
}
trap cleanup EXIT

NVS_ARGS=()
if [ -n "$KID" ]; then
    echo "======================================================================"
    echo " DANGER: writing NVS at 0x9000 WIPES the whole 24 KB NVS partition"
    echo " (event cache + any NVS keys).  Network config falls back to the"
    echo " local_secrets.h defines, so this is safe for the RoboDog boards."
    echo "======================================================================"
    NVS_WORK="$(mktemp -d /tmp/robodog_nvs.XXXXXX)"
    # Namespace must be "robodog" (NVS_NS in robodog.c) for config_load() to
    # read key "kid_name".  The plan sketch used "kid_name" as the namespace
    # name which would NOT match the firmware reader — corrected here.
    cat > "$NVS_WORK/kid_nvs.csv" <<EOF
key,type,encoding,value
robodog,namespace,,
kid_name,data,string,$KID
EOF
    echo "generating NVS image (kid_name='$KID') via IDF nvs_partition_gen.py"
    docker run --rm \
        -v "$APP:/workspace" -w /workspace \
        -v "$NVS_WORK:/nvs" \
        "$IDF_IMG" \
        python3 /opt/esp/idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
            generate /nvs/kid_nvs.csv /nvs/nvs.bin 0x6000
    NVS_ARGS=(0x9000 "$NVS_WORK/nvs.bin")
elif [ "$KID_FLAG" -eq 1 ]; then
    # Explicit empty --kid-name "": wipe the partition back to family mode by
    # writing an NVS image with the namespace but no kid_name key.
    echo "======================================================================"
    echo " DANGER: writing an EMPTY NVS at 0x9000 WIPES the whole 24 KB"
    echo " partition (clears kid_name + event cache -> back to family mode)."
    echo "======================================================================"
    NVS_WORK="$(mktemp -d /tmp/robodog_nvs.XXXXXX)"
    printf 'key,type,encoding,value\nrobodog,namespace,,\n' > "$NVS_WORK/kid_nvs.csv"
    echo "generating EMPTY NVS image (family mode) via IDF nvs_partition_gen.py"
    docker run --rm \
        -v "$APP:/workspace" -w /workspace \
        -v "$NVS_WORK:/nvs" \
        "$IDF_IMG" \
        python3 /opt/esp/idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
            generate /nvs/kid_nvs.csv /nvs/nvs.bin 0x6000
    NVS_ARGS=(0x9000 "$NVS_WORK/nvs.bin")
fi

cd "$APP/build"
echo "flashing app + partition table from @flash_args ..."
if [ "${#NVS_ARGS[@]}" -eq 0 ]; then
    exec python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
        --before default_reset --after hard_reset write_flash @flash_args
fi
python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    --before default_reset --after hard_reset write_flash \
    @flash_args "${NVS_ARGS[@]}"
if [ -n "$KID" ]; then
    echo "==== board $PORT assigned kid_name='$KID' (NVS written at 0x9000) ===="
else
    echo "==== board $PORT NVS cleared to family mode (empty NVS at 0x9000) ===="
fi
