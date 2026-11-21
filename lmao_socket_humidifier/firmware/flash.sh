#!/usr/bin/env bash
# Flash the Humidifier Atom Socket firmware to the M5Stack Atom Lite and
# provision its NVS (WiFi + mDNS host + API token + default lease).
#
#   LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
#   bazel run //lmao_socket_humidifier:flash_firmware -- --port /dev/ttyUSB0 \
#       --mdns-host humidifier-baby --device-name "baby room" \
#       --api-token "$(openssl rand -hex 16)"
#
# --device-name is BOTH the SensorReport.node_id (what the controller's
# --env-node matches) and the announce display name (the server's contact
# book), so it must be the room's name exactly.
#
# --port is REQUIRED (no silent default): the Atom Socket shares the
# /dev/ttyUSB* space with the RNode, and the RNode must NEVER be flashed
# (AGENTS.md).  Run tools/identify_device.py first.
#
# Credentials and the token are NEVER hardcoded: they are written to the
# 24 KB NVS partition at 0x9000.  Writing NVS wipes the partition (desired:
# a re-provisioned socket keeps no stale token).  An unprovisioned socket
# refuses relay commands (fails closed).
#
# --keep-nvs reflashes the app ONLY and leaves the device's NVS untouched, so
# an already-provisioned socket keeps its WiFi creds, mDNS host, device name
# and API token.  That needs no secrets at all: use it to put new firmware on
# a socket that is already reporting (the usual case).
#
# Flashing goes through the ESP-IDF Docker image (the sanctioned Atom Lite
# path — the same one the Sprout uses), at 115200 baud: the ESP32-PICO-D4
# embedded flash is unreliable at 460800+ (AGENTS.md).
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/lmao_socket_humidifier/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
REPO_ROOT="$(cd "$APP/../.." && pwd)"
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"
# Same staging dir build.sh uses (inside the pinned RTReticulum checkout).
RTR="${RTRETICULUM_ROOT:-$REPO_ROOT/smart_irrigation/native-client/.rtreticulum}"
DEST="$RTR/firmware/humidifier_socket"

PORT="${FLASH_PORT:-}"
BAUD="${FLASH_BAUD:-115200}"
WIFI_SSID="${LMAO_WIFI_SSID:-}"
WIFI_PASS="${LMAO_WIFI_PASS:-}"
MDNS_HOST="${HF_MDNS_HOST:-humidifier}"
API_TOKEN="${HF_API_TOKEN:-}"
LEASE_DEFAULT="${HF_LEASE_DEFAULT:-300}"
SERVER_HOST="${HF_SERVER_HOST:-tp4}"
SERVER_PORT="${HF_SERVER_PORT:-4246}"
DEVICE_NAME="${HF_DEVICE_NAME:-}"
KEEP_NVS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --port)         PORT="${2:-}";         shift 2 || true;;
        --baud)         BAUD="${2:-}";         shift 2 || true;;
        --wifi-ssid)    WIFI_SSID="${2:-}";    shift 2 || true;;
        --wifi-pass)    WIFI_PASS="${2:-}";    shift 2 || true;;
        --mdns-host)    MDNS_HOST="${2:-}";    shift 2 || true;;
        --api-token)    API_TOKEN="${2:-}";    shift 2 || true;;
        --lease-default) LEASE_DEFAULT="${2:-}"; shift 2 || true;;
        --server-host)  SERVER_HOST="${2:-}";  shift 2 || true;;
        --server-port)  SERVER_PORT="${2:-}";  shift 2 || true;;
        --device-name)  DEVICE_NAME="${2:-}";  shift 2 || true;;
        --keep-nvs)     KEEP_NVS=1;            shift 1 || true;;
        -h|--help)
            echo "usage: $0 --port X --wifi-ssid S --wifi-pass P --api-token T" >&2
            echo "          --device-name NAME [--server-host H] [--server-port P]" >&2
            echo "          [--mdns-host NAME] [--lease-default SECS] [--baud 115200]" >&2
            echo "" >&2
            echo "  reflash an already-provisioned socket (NVS untouched):" >&2
            echo "    $0 --port X --device-name NAME --keep-nvs" >&2
            exit 0;;
        *) echo "unknown argument: $1" >&2; exit 2;;
    esac
done

if [ -z "$PORT" ]; then
    echo "ERROR: --port X is required (no default). USB serial devices:" >&2
    python3 - <<'PY' >&2 || true
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    if p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
        print(f"  {p.device:<12} {p.description}")
PY
    echo "Run tools/identify_device.py to classify by type, then re-run." >&2
    exit 3
fi
if [ "$KEEP_NVS" = 0 ]; then
    if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PASS" ]; then
        echo "error: WiFi credentials are required (nothing is compiled in)." >&2
        echo "  pass --wifi-ssid/--wifi-pass, or export LMAO_WIFI_SSID/LMAO_WIFI_PASS." >&2
        echo "  Reflashing a socket that is already provisioned? use --keep-nvs." >&2
        exit 2
    fi
    if [ -z "$API_TOKEN" ]; then
        echo "error: --api-token is required: the HTTP API fails closed without it." >&2
        echo "  generate one with: openssl rand -hex 16" >&2
        echo "  Reflashing a socket that is already provisioned? use --keep-nvs." >&2
        exit 2
    fi
fi
case "$LEASE_DEFAULT" in
    ''|*[!0-9]*) echo "invalid --lease-default '$LEASE_DEFAULT'" >&2; exit 2;;
esac
if [ -z "$DEVICE_NAME" ]; then
    echo "error: --device-name is required: it is the SensorReport.node_id the" >&2
    echo "  controller pins with --env-node, and the announce display name." >&2
    exit 2
fi
# Rebuild when the staged image was built for a different room (the node_id
# and the pinned identity are baked at build time, so a stale image would
# report into another room's series) or for a different IDENTITY (a socket
# flashed with the room's RoboDog-home key would share that delivery hash,
# and two devices on one hash corrupt LXMF delivery).
STAGED_NAME="$(sed -n 's/^#define HF_DEVICE_NAME_DEFAULT "\(.*\)"/\1/p' \
    "$DEST/main/lmao_config.h" 2>/dev/null | head -1)"
STAGED_IDENTITY="$(sed -n 's/^#define HF_IDENTITY_NAME_DEFAULT "\(.*\)"/\1/p' \
    "$DEST/main/lmao_config.h" 2>/dev/null | head -1)"
IDENTITY_NAME="${HF_IDENTITY_NAME:-humidifier-baby}"
if [ ! -f "$DEST/build/flash_args" ] || [ "$STAGED_NAME" != "$DEVICE_NAME" ] \
    || [ "$STAGED_IDENTITY" != "$IDENTITY_NAME" ]; then
    echo "no matching build at $DEST (staged room='${STAGED_NAME:-none}'" \
         "identity='${STAGED_IDENTITY:-none}') — building for room='$DEVICE_NAME'" \
         "identity='$IDENTITY_NAME'"
    HF_DEVICE_NAME="$DEVICE_NAME" HF_IDENTITY_NAME="$IDENTITY_NAME" \
        "$APP/build.sh" || exit 1
fi

WORK="$(mktemp -d /tmp/humidifier_nvs.XXXXXX)"
trap 'rm -rf "${WORK:-}"' EXIT

if [ "$KEEP_NVS" = 1 ]; then
    # App-only reflash: idf.py flash writes bootloader + partition table +
    # app, never NVS, so WiFi/host/token/name all survive.  The staged build
    # must still be for this room (checked above).
    echo "flashing app to $PORT @ $BAUD (NVS preserved) ..."
    docker run --rm -v "$RTR:/repo" -w /repo/firmware/humidifier_socket \
        --device "$PORT:$PORT" -e IDF_TARGET=esp32 "$IDF_IMG" \
        bash -lc "git config --global --add safe.directory /repo; idf.py -p $PORT -b $BAUD flash" || exit 1
    echo "==== $PORT reflashed (app only): device='$DEVICE_NAME' — NVS untouched ===="
    echo "    WiFi, mdns_host, api_token, server_host/port and lease_default are unchanged."
    echo "    Verify: curl -s http://<mdns_host>.local/status | grep directive_polls"
    exit 0
fi

csv_quote() {
    case "$1" in
        *[\",]*) printf '"%s"' "${1//\"/\"\"}";;
        *) printf '%s' "$1";;
    esac
}
cat > "$WORK/nvs.csv" <<EOF
key,type,encoding,value
humidifier,namespace,,
wifi_ssid,data,string,$(csv_quote "$WIFI_SSID")
wifi_pass,data,string,$(csv_quote "$WIFI_PASS")
mdns_host,data,string,$(csv_quote "$MDNS_HOST")
api_token,data,string,$(csv_quote "$API_TOKEN")
lease_default,data,u32,$LEASE_DEFAULT
server_host,data,string,$(csv_quote "$SERVER_HOST")
server_port,data,u16,$SERVER_PORT
device_name,data,string,$(csv_quote "$DEVICE_NAME")
EOF

echo "generating NVS image via IDF nvs_partition_gen.py"
docker run --rm -v "$WORK:/nvs" "$IDF_IMG" \
    python3 /opt/esp/idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate /nvs/nvs.csv /nvs/nvs.bin 0x6000 >/dev/null

echo "flashing app + partition table + NVS to $PORT @ $BAUD ..."
# The build tree lives inside the pinned RTReticulum checkout, so mount the
# checkout at the same path it had at build time (/repo) and flash from the
# app dir — idf.py refuses to flash when the build dir belongs to another
# project path.
docker run --rm -v "$RTR:/repo" -v "$WORK:/nvs" -w /repo/firmware/humidifier_socket \
    --device "$PORT:$PORT" -e IDF_TARGET=esp32 "$IDF_IMG" \
    bash -lc "git config --global --add safe.directory /repo; idf.py -p $PORT -b $BAUD flash && \
              python -m esptool --chip esp32 -p $PORT -b $BAUD \
                  --before default_reset --after hard_reset \
                  write_flash 0x9000 /nvs/nvs.bin" || exit 1

echo "==== $PORT flashed: mdns='$MDNS_HOST' device='$DEVICE_NAME' lease_default=${LEASE_DEFAULT}s ===="
echo "    server ${SERVER_HOST}:${SERVER_PORT} (SensorReport.node_id='$DEVICE_NAME')"
echo "    API: http://$MDNS_HOST.local/status   (token in X-Auth-Token)"
