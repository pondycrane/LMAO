#!/usr/bin/env bash
# Flash the RoboDog home client firmware to the Waveshare ESP32-S3 RLCD-4.2,
# button-free, and provision its NVS (device name + WiFi + server target).
#
#   LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
#   bazel run //robodog_home_client:flash_firmware -- \
#       --device-name "main bedroom" --port /dev/ttyACM0
#
# Credentials are NEVER hardcoded: --wifi-ssid/--wifi-pass (or
# LMAO_WIFI_SSID/LMAO_WIFI_PASS in the environment) are required and are
# written to the 24 KB NVS partition at 0x9000.  Writing NVS wipes the
# partition; the identity is baked at build time, so the delivery hash is
# unaffected.
#
# Uses the SHARED host-esptool plumbing (robodog_client/flash_common.sh) that
# the calendar/hermes/claw flashes use: Docker's ttyACM RTS passthrough breaks
# the button-free reset, so esptool runs on the host.
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_home_client"
    ROBODOG="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
else
    APP="$(cd "$(dirname "$0")/.." && pwd)"
    ROBODOG="$(cd "$APP/../robodog_client" && pwd)"
fi
. "$ROBODOG/flash_common.sh"
rd_flash_init "$APP"
# This firmware's build lives in its own staged RTReticulum app dir.
RD_APP="$APP/firmware"
RTR="${RTRETICULUM_ROOT:-$(cd "$APP/../smart_irrigation/native-client" 2>/dev/null && pwd)/.rtreticulum}"
BUILD="$RTR/firmware/robodog_home/build"

DEVICE_NAME="${RH_DEVICE_NAME:-}"
WIFI_SSID="${LMAO_WIFI_SSID:-}"
WIFI_PASS="${LMAO_WIFI_PASS:-}"
SERVER_HOST="${LMAO_SERVER_HOST:-tp4}"
SERVER_PORT="${LMAO_SERVER_PORT:-4246}"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)        rd_need_value --port $# "${2:-}"; RD_PORT="$2"; shift 2;;
        --device-name) rd_need_value --device-name $# "${2:-}"; DEVICE_NAME="$2"; shift 2;;
        --wifi-ssid)   rd_need_value --wifi-ssid $# "${2:-}"; WIFI_SSID="$2"; shift 2;;
        --wifi-pass)   rd_need_value --wifi-pass $# "${2:-}"; WIFI_PASS="$2"; shift 2;;
        --server-host) rd_need_value --server-host $# "${2:-}"; SERVER_HOST="$2"; shift 2;;
        --server-port) rd_need_value --server-port $# "${2:-}"; SERVER_PORT="$2"; shift 2;;
        *) echo "unknown argument: $1" >&2; exit 2;;
    esac
done

if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PASS" ]; then
    echo "error: WiFi credentials are required (no defaults are compiled in)." >&2
    echo "  pass --wifi-ssid/--wifi-pass, or export LMAO_WIFI_SSID/LMAO_WIFI_PASS." >&2
    exit 2
fi
if [ -z "$DEVICE_NAME" ]; then
    echo "error: --device-name is required (e.g. \"main bedroom\")." >&2
    exit 2
fi
if [ ! -f "$BUILD/flash_args" ]; then
    echo "error: no build at $BUILD — run //robodog_home_client:build_firmware first" >&2
    exit 1
fi

rd_require_host_esptool
rd_nvs_tmpdir

echo "provisioning NVS: device='$DEVICE_NAME' server=$SERVER_HOST:$SERVER_PORT"
# The IDF CSV generator takes values verbatim; quote only when a comma or
# quote would break the field.
csv_quote() {
    case "$1" in
        *[\",]*) printf '"%s"' "${1//\"/\"\"}";;
        *) printf '%s' "$1";;
    esac
}
cat > "$RD_NVS_WORK/nvs.csv" <<EOF
key,type,encoding,value
robodog_home,namespace,,
device_name,data,string,$(csv_quote "$DEVICE_NAME")
wifi_ssid,data,string,$(csv_quote "$WIFI_SSID")
wifi_pass,data,string,$(csv_quote "$WIFI_PASS")
server_host,data,string,$(csv_quote "$SERVER_HOST")
server_port,data,u16,$SERVER_PORT
EOF

echo "generating NVS image via IDF nvs_partition_gen.py"
rd_nvs_generate "$RD_NVS_WORK/nvs.csv" "$RD_NVS_WORK/nvs.bin"

echo "flashing app + partition table + NVS ..."
rd_flash_write "$BUILD" 0x9000 "$RD_NVS_WORK/nvs.bin"

echo "==== $RD_PORT flashed: device_name='$DEVICE_NAME', server=$SERVER_HOST:$SERVER_PORT ===="
