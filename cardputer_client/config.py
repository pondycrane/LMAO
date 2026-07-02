"""
µReticulum configuration for M5Stack Cardputer ADV with LoRa antenna.

The Cardputer uses its onboard SX1262 LoRa radio directly (via SPI)
rather than an external RNode. This config is loaded by the urns
µReticulum port when running on MicroPython.

Edit WIFI_SSID and WIFI_PASS to match your network.
"""

from lora_boards import LORA_BOARDS

# ---- Node settings ----
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASS = "YOUR_WIFI_PASSWORD"
NODE_NAME = "LMAO_Cardputer"

# DEBUG levels: 0 = silent, 1 = messages & announces, 2 = full debug
DEBUG = 2

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
        {
            "type": "LoRaInterface",
            "board": "cardputer_adv",   # pinout preset in lora_boards.py
            "name": "LoRa",
            "enabled": True,
            "freq_khz": 868000,         # 868 MHz (EU); 915000 for US
            "sf": 7,                     # SF7 fastest
            "bw": "125",                 # 125 kHz
            "coding_rate": 5,           # 4:5
            "tx_power": 14,             # dBm
            "preamble_len": 8,
            "crc_en": True,
            "syncword": 0x1424,         # Reticulum default syncword
        },
    ],
}
