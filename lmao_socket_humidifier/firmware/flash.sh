#!/usr/bin/env bash
# Flash the Humidifier Atom Socket firmware to the M5Stack Atom Lite and
# provision its NVS (WiFi + mDNS host + API token + default lease).
#
#   LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
#   bazel run //humidifier:flash_firmware -- --port /dev/ttyUSB0 \
#       --mdns-host humidifier --api-token "$(openssl rand -hex 16)"
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
DEST="$APP/.idf/lmao_socket_humidifier"

PORT="${FLASH_PORT:-}"
BAUD="${FLASH_BAUD:-115200}"
WIFI_SSID="${LMAO_WIFI_SSID:-}"
WIFI_PASS="${LMAO_WIFI_PASS:-}"
MDNS_HOST="${HF_MDNS_HOST:-humidifier}"
API_TOKEN="${HF_API_TOKEN:-}"
LEASE_DEFAULT="${HF_LEASE_DEFAULT:-300}"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)         PORT="${2:-}";         shift 2 || true;;
        --baud)         BAUD="${2:-}";         shift 2 || true;;
        --wifi-ssid)    WIFI_SSID="${2:-}";    shift 2 || true;;
        --wifi-pass)    WIFI_PASS="${2:-}";    shift 2 || true;;
        --mdns-host)    MDNS_HOST="${2:-}";    shift 2 || true;;
        --api-token)    API_TOKEN="${2:-}";    shift 2 || true;;
        --lease-default) LEASE_DEFAULT="${2:-}"; shift 2 || true;;
        -h|--help)
            echo "usage: $0 --port X --wifi-ssid S --wifi-pass P --api-token T" >&2
            echo "          [--mdns-host NAME] [--lease-default SECS] [--baud 115200]" >&2
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
if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PASS" ]; then
    echo "error: WiFi credentials are required (nothing is compiled in)." >&2
    echo "  pass --wifi-ssid/--wifi-pass, or export LMAO_WIFI_SSID/LMAO_WIFI_PASS." >&2
    exit 2
fi
if [ -z "$API_TOKEN" ]; then
    echo "error: --api-token is required: the HTTP API fails closed without it." >&2
    echo "  generate one with: openssl rand -hex 16" >&2
    exit 2
fi
case "$LEASE_DEFAULT" in
    ''|*[!0-9]*) echo "invalid --lease-default '$LEASE_DEFAULT'" >&2; exit 2;;
esac
if [ ! -f "$DEST/build/flash_args" ]; then
    echo "error: no build at $DEST — run //humidifier:build_firmware first" >&2
    exit 1
fi

WORK="$(mktemp -d /tmp/humidifier_nvs.XXXXXX)"
trap 'rm -rf "${WORK:-}"' EXIT

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
EOF

echo "generating NVS image via IDF nvs_partition_gen.py"
docker run --rm -v "$WORK:/nvs" "$IDF_IMG" \
    python3 /opt/esp/idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate /nvs/nvs.csv /nvs/nvs.bin 0x6000 >/dev/null

echo "flashing app + partition table + NVS to $PORT @ $BAUD ..."
docker run --rm -v "$DEST:/app" -v "$WORK:/nvs" -w /app \
    --device "$PORT:$PORT" -e IDF_TARGET=esp32 "$IDF_IMG" \
    bash -lc "idf.py -p $PORT -b $BAUD flash && \
              python -m esptool --chip esp32 -p $PORT -b $BAUD \
                  --before default_reset --after hard_reset \
                  write_flash 0x9000 /nvs/nvs.bin" || exit 1

echo "==== $PORT flashed: mdns='$MDNS_HOST' lease_default=${LEASE_DEFAULT}s ===="
echo "    API: http://$MDNS_HOST.local/status   (token in X-Auth-Token)"
