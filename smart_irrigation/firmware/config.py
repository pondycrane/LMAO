"""
µReticulum configuration for Sprout — LMAO Smart Irrigation Node.

Sprout = M5Stack Atom Lite on the DTU LoRaWAN base (A152-EU868). The radio is
the RAK3172/STM32WLE5CC in LoRa P2P mode over UART AT (AT+PSEND / +EVT:RXP2P),
driven by the urns ``DtuInterface`` (see lib/urns/interfaces/dtu.py). Radio
parameters MUST match the LMAO server's RNode (same as the Cardputer SX1262):
868 MHz, BW 125 k, SF 7, CR 4:5, preamble 24, syncword 0x1424.
"""

# ---- Node settings ----
# LoRa-only — no WiFi path for the LMAO stack.
NODE_NAME = "LMAO_Sprout"

# DEBUG levels: 0 = silent, 1 = messages & announces, 2 = full radio debug.
# Keep at 1 for unattended ops (level 2 floods the USB-CDC and can lock the
# REPL — same constraint as the Cardputer, issue #81).
DEBUG = 1

# Destination hash of the server's lxmf.delivery destination (hex of a
# 16-byte hash). None = don't send. Injected at flash/run time so Sprout
# knows where to deliver SensorReports.
DEST_HASH = None

# Send interval in seconds for SensorReports. Minimum 10s to avoid LoRa
# congestion.
INTERVAL_SECONDS = 60

# ---- Reticulum config ----
CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
    "lora_boards": {},
    "interfaces": [
        # ---- Sprout DTU LoRa radio (RAK3172 P2P over UART AT) ----
        {
            "type": "DtuInterface",
            "name": "DTU P2P LoRa",
            "enabled": True,
            # Atom <-> DTU base UART (9-pin stack): TX=G22, RX=G19.
            "tx_pin": 22,
            "rx_pin": 19,
            "baud": 115200,
            # LMAO mesh radio parameters (match server RNode / Cardputer).
            "freq": 868_000_000,
            "sf": 7,
            "bw": 0,  # 125 kHz
            "cr": 0,  # 4/5
            "ppl": 24,
            "syncword": "1424",  # 0x1424 (Reticulum default)
            "txp": 17,  # dBm
        },
    ],
}
