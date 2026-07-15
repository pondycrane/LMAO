"""
µReticulum configuration for M5Stack Cardputer ADV with LoRa antenna.

The Cardputer uses its onboard SX1262 LoRa radio directly (via SPI)
rather than an external RNode. This config is loaded by the urns
µReticulum port when running on MicroPython.

Edit WIFI_SSID and WIFI_PASS to match your network.

For other SX1262/SX1276 boards, define a new pinout preset in
``lora_boards.py`` and reference it via the ``board`` key in the
interface config below.
"""

from lora_boards import LORA_BOARDS

# ---- Node settings ----
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASS = "YOUR_WIFI_PASSWORD"
NODE_NAME = "LMAO_Cardputer"

# DEBUG levels: 0 = silent, 1 = messages & announces, 2 = full debug
DEBUG = 2

# Destination server identity hash (hex string of 16-byte hash).
# Set to None (default) to skip sending.  The E2E test or flash tool
# can inject the server hash before uploading so the Cardputer knows
# where to send messages.
#
# Format: hex string, e.g. "a1b2c3d4e5f6..." (32 hex chars).
# main.py converts this to bytes at runtime for the urns LXMF router.
DEST_HASH = None

# Send interval in seconds — how often the Cardputer transmits sensor data.
# Default 60s = 1 reading per minute. Minimum 10s to avoid LoRa congestion.
INTERVAL_SECONDS = 60

# External humidity/temperature sensor type on the Grove I2C port.
# Supported values: "DHT20" (Grove DHT20 / AHT20), None (no sensor).
# When None, only ESP32 die temperature is sent.
SENSOR_TYPE = None

# I2C address of the external sensor (0x38 is the default for DHT20/AHT20).
SENSOR_I2C_ADDR = 0x38

# ---- Reticulum config ----
CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
    "lora_boards": LORA_BOARDS,
    "probe": {
        "enabled": False,
        "app_name": "urns",
        "aspect": "probe",
        "announce_interval": 60 * 60,
    },
    "time_sync": {
        "enabled": True,
        "trusted_nodes": [],
        "min_sources": 2,
        "tolerance": 120,
    },
    "interfaces": [
        # ---- WiFi UDP (for setup / debugging) ----
        # Comment out if using LoRa-only mode.
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "enabled": True,
            "listen_ip": "0.0.0.0",
            "listen_port": 4242,
            "forward_ip": "255.255.255.255",
            "forward_port": 4242,
        },
        # ---- Cardputer onboard SX1262 LoRa radio ----
        # Uses the micropython-lib lora-sx126x driver with the following fixes:
        #   1. JTAG pins (GPIO39=MTCK, GPIO40=MTDO) reclaimed before SPI init
        #   2. TCXO startup time override (5000us) for Cap LoRa-1262 module
        #   3. TCXO_OPTIONAL fallback — retries without TCXO config if init fails
        # If issues persist, set enabled=False to run in WiFi-only mode.
        {
            "type": "LoRaInterface",
            "board": "cardputer_adv",  # pinout preset in lora_boards.py
            "name": "LoRa",
            "enabled": True,
            "freq_khz": 868000,  # 868 MHz (EU); 915000 for US
            "sf": 7,  # SF7 fastest
            "bw": "125",  # 125 kHz
            "coding_rate": 5,  # 4:5
            "tx_power": 14,  # dBm
            "preamble_len": 8,
            "crc_en": True,
            "syncword": 0x1424,  # Reticulum default syncword
        },
    ],
}
