"""
µReticulum configuration for the RoboDog — Waveshare ESP32-S3 RLCD-4.2
family-calendar display client.

Connects to the LMAO server over an RNS **TCP client** interface (LAN, port
4246) instead of LoRa: the server's ``TCPServerInterface`` is the other end of
the HDLC-framed TCP link this config's ``TCPClientInterface`` dials.

``install_all.py`` patches the DEST_HASH / LMAO_SERVER_HOST / WIFI_SSID /
WIFI_PASS assignments below at flash time by regex (source tree is never
modified).  Keep each of those four as a single ``NAME = value`` line so the
patch always matches exactly one occurrence.
"""

# ---- Node settings -------------------------------------------------------
NODE_NAME = "LMAO_RoboDog"

# WiFi station credentials.  None = stay offline (boot shows the cached
# calendar from SD and only announces nothing / logs).
WIFI_SSID = None
WIFI_PASS = None

# LMAO server LAN endpoint for the RNS TCP client interface.
LMAO_SERVER_HOST = "192.168.50.67"
LMAO_SERVER_PORT = 4246

# Destination hash of the server's lxmf.delivery destination (hex string of
# a 16-byte hash) — the value printed by the server as its delivery hash.
# None = announce-only: the client boots, prints its own hash for the operator
# to allow-list, and skips calendar fetches until this is set (e.g. by
# install_all at flash time).
DEST_HASH = None

# How often the client re-fetches the family calendar (seconds).  1800 = 30 min.
SYNC_INTERVAL_SECONDS = 1800

# How long to wait for the server's CalendarBundle reply before giving up (s).
REPLY_TIMEOUT_SECONDS = 30

# Family calendar the display shows (CalendarRequest.calendar_id).
CALENDAR_ID = "family"

# Battery duty cycle: when True, the idle window between syncs uses
# machine.lightsleep() (the VM is suspended — replies only arrive during the
# post-request listen window).  When False (default) the event loop stays
# alive and merely sleeps, so announces/other traffic are always received.
LIGHT_SLEEP = False

# DEBUG levels: 0 = silent, 1 = messages & announces, 2 = full debug.
DEBUG = 1

# ---- Optional microSD cache ---------------------------------------------
# Persists the last watermark + events as JSON on /sd so the display boots
# showing yesterday's/stale calendar while WiFi is down.  Everything SD is
# fault-tolerant: any failure (no card, bad mount) silently soft-disables it.
# Pin values below are placeholders — verify against the RLCD board
# schematic before enabling (set SD_CS to a GPIO number).  None = disabled.
SD_CS = None
SD_SCK = 12
SD_MOSI = 11
SD_MISO = 13
SD_MOUNT = "/sd"
# File names written to the mounted SD card (kept versioned for easy debugging).
SD_DATA_FILE = "/sd/robodog_calendar.json"

# ---- Reticulum storage ---------------------------------------------------
# urns persists the identity + known destinations under this path.  The
# default of standard flash boot ("/") leaves it at /rns; main.py ensures the
# directory exists before Reticulum init (fault-tolerant os.mkdir).
RNS_CONFIG_PATH = "/rns/config.json"

# ---- Reticulum config ----------------------------------------------------
# Single TCP client interface — mirrors cardputer_client/config.py's shape but
# with no LoRa radio.  target_host/target_port reference the module constants
# above so install_all's LMAO_SERVER_HOST patch flows straight through.
CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
    "probe": {
        "enabled": False,
        "app_name": "urns",
        "aspect": "probe",
        "announce_interval": 60 * 60,
    },
    "time_sync": {
        "enabled": False,
        "trusted_nodes": [],
        "min_sources": 2,
        "tolerance": 120,
    },
    "interfaces": [
        {
            "type": "TCPClientInterface",
            "name": "RNS-TCP",
            "enabled": True,
            "target_host": LMAO_SERVER_HOST,
            "target_port": LMAO_SERVER_PORT,
        },
    ],
}
