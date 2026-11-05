#!/usr/bin/env bash
# Flash robodog_client (the family-calendar firmware) to the Waveshare
# ESP32-S3 RLCD, button-free, and optionally provision its NVS config.
#
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM0]
#   bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
#   LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
#     bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
#
# WiFi credentials are NEVER committed and never baked into the image:
#   * --wifi-ssid/--wifi-pass (or LMAO_WIFI_SSID/LMAO_WIFI_PASS), else
#   * the local (gitignored) main/local_secrets.h defines, when that file exists.
# They are written into the 24 KB NVS partition at 0x9000, which the firmware
# reads at boot (rd_store.h: namespace "robodog", keys wifi_ssid/wifi_pass/
# server_host).  Without either source the board boots with no WiFi.
#
# `--kid-name <kid>` (non-empty) additionally writes `kid_name`: the board
# becomes a kid-dedicated panel.  Writing NVS WIPES the whole 24 KB partition
# (event cache + any NVS keys), so the image ALWAYS carries the WiFi/server
# config — otherwise a kid-panel flash would silently unnetwork the board.
# An explicit `--kid-name ""` writes the config WITHOUT kid_name -> clears a
# previously-assigned board back to family mode.  No NVS flags at all leaves
# NVS untouched.
#
# Any NVS write therefore requires credentials from one of the two sources
# above; with neither, the script refuses rather than flashing a board that
# boots with no network.
#
# Host esptool (Docker's ttyACM RTS passthrough breaks the button-free reset);
# the shared plumbing lives in flash_common.sh.
set -euo pipefail
# `bazel run` execs the bazel-bin symlink, whose dirname is NOT the source
# package dir — resolve the source tree from BUILD_WORKSPACE_DIRECTORY first.
APP="${BUILD_WORKSPACE_DIRECTORY:+$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
APP="${APP:-$(cd "$(dirname "$0")" && pwd)}"
. "$APP/flash_common.sh"
rd_flash_init "$APP"

KID=""
KID_FLAG=0
# An EXPLICIT NVS flag is what writes NVS.  A credential picked up from the
# environment or the local local_secrets.h is a VALUE source, not a request:
# keying the wipe off "we happen to have a SSID" made a plain
# `flash_firmware --port ...` wipe the 24 KB partition.
NVS_FLAG=0
WIFI_SSID="${LMAO_WIFI_SSID:-}"
WIFI_PASS="${LMAO_WIFI_PASS:-}"
SERVER_HOST="${LMAO_SERVER_HOST:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            rd_need_value "--port" $# "${2:-}" "default: /dev/ttyACM0"
            RD_PORT="$2"
            shift 2
            ;;
        --kid-name)
            # Only an explicit `--kid-name ""` clears the board back to family.
            rd_need_value "--kid-name" $# "${2:-}" 'use --kid-name "" to clear to family'
            KID="$2"; KID_FLAG=1; NVS_FLAG=1; shift 2
            ;;
        --wifi-ssid)
            rd_need_value "--wifi-ssid" $# "${2:-}"
            WIFI_SSID="$2"; NVS_FLAG=1; shift 2
            ;;
        --wifi-pass)
            rd_need_value "--wifi-pass" $# "${2:-}"
            WIFI_PASS="$2"; NVS_FLAG=1; shift 2
            ;;
        --server-host)
            rd_need_value "--server-host" $# "${2:-}"
            SERVER_HOST="$2"; NVS_FLAG=1; shift 2
            ;;
        *)
            echo "unknown argument: $1 (expected --port, --kid-name, --wifi-ssid," >&2
            echo "  --wifi-pass, --server-host)" >&2
            exit 2
            ;;
    esac
done

# Local fallback: the gitignored main/local_secrets.h (created by hand, never
# committed).  Only read the #define VALUES — the file is not sourced/evaled.
SECRETS="$APP/main/local_secrets.h"
secrets_define() {
    [ -f "$SECRETS" ] || return 0
    sed -n "s/^#define[[:space:]]\+$1[[:space:]]\+\"\(.*\)\".*/\1/p" "$SECRETS" | head -1
}
if [ -z "$WIFI_SSID" ]; then WIFI_SSID="$(secrets_define RD_WIFI_SSID_DEFAULT)"; fi
if [ -z "$WIFI_PASS" ]; then WIFI_PASS="$(secrets_define RD_WIFI_PASS_DEFAULT)"; fi
if [ -z "$SERVER_HOST" ]; then SERVER_HOST="$(secrets_define RD_SERVER_HOST_DEFAULT)"; fi
SERVER_HOST="${SERVER_HOST:-tp4}"

rd_require_host_esptool

# The IDF CSV generator takes values verbatim; quote only when a comma or
# quote would break the field.
csv_quote() {
    case "$1" in
        *[\",]*) printf '"%s"' "${1//\"/\"\"}";;
        *) printf '%s' "$1";;
    esac
}

NVS_ARGS=()
if [ "$NVS_FLAG" -eq 1 ]; then
    if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PASS" ]; then
        echo "error: writing NVS needs WiFi credentials (the board would boot" >&2
        echo "  with no network). Pass --wifi-ssid/--wifi-pass, export" >&2
        echo "  LMAO_WIFI_SSID/LMAO_WIFI_PASS, or create the local" >&2
        echo "  (gitignored) $SECRETS." >&2
        exit 2
    fi
    echo "======================================================================"
    echo " DANGER: writing NVS at 0x9000 WIPES the whole 24 KB NVS partition"
    echo " (event cache + any NVS keys).  The new image carries the WiFi/server"
    echo " config, so the board stays networked; the calendar cache refetches."
    echo "======================================================================"
    rd_nvs_tmpdir
    {
        echo "key,type,encoding,value"
        # Namespace must be "robodog" (RD_NVS_NAMESPACE in rd_store.h) for the
        # firmware's config_load() to read these keys.
        echo "robodog,namespace,,"
        echo "wifi_ssid,data,string,$(csv_quote "$WIFI_SSID")"
        echo "wifi_pass,data,string,$(csv_quote "$WIFI_PASS")"
        echo "server_host,data,string,$(csv_quote "$SERVER_HOST")"
        # An explicit `--kid-name ""` clears the assignment (family mode): the
        # key is simply omitted from the image.
        if [ "$KID_FLAG" -eq 1 ] && [ -n "$KID" ]; then
            echo "kid_name,data,string,$(csv_quote "$KID")"
        fi
    } > "$RD_NVS_WORK/nvs.csv"

    echo "generating NVS image via IDF nvs_partition_gen.py"
    echo "  keys: wifi_ssid, wifi_pass, server_host=$SERVER_HOST${KID:+, kid_name=$KID}"
    rd_nvs_generate "$RD_NVS_WORK/nvs.csv" "$RD_NVS_WORK/nvs.bin"
    NVS_ARGS=(0x9000 "$RD_NVS_WORK/nvs.bin")
fi

echo "flashing app + partition table from @flash_args ..."
if [ "${#NVS_ARGS[@]}" -eq 0 ]; then
    echo "==== board $RD_PORT: NVS untouched (no --kid-name/--wifi-*/--server-host) ===="
    # exec: the esptool process becomes this script (no trailing shell).
    cd "$RD_APP/build"
    exec python3 -m esptool --chip esp32s3 -p "$RD_PORT" -b "$RD_BAUD" \
        --before default_reset --after hard_reset write_flash @flash_args
fi
rd_flash_write "$RD_APP/build" "${NVS_ARGS[@]}"
if [ -n "$KID" ]; then
    echo "==== board $RD_PORT: kid_name='$KID', wifi+server provisioned (NVS at 0x9000) ===="
elif [ "$KID_FLAG" -eq 1 ]; then
    echo "==== board $RD_PORT: NVS cleared to family mode (wifi+server provisioned) ===="
else
    echo "==== board $RD_PORT: wifi+server provisioned (NVS at 0x9000) ===="
fi
