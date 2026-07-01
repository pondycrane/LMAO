"""
µReticulum configuration for M5Stack Cardputer ADV with LoRa antenna.

The Cardputer uses its onboard SX1262 LoRa radio directly (via SPI)
rather than an external RNode. This config is loaded by µReticulum
when running on MicroPython.

NOTE: This config targets the SX1262 (Cardputer ADV default).
For SX1276-based boards, remove 'busy_pin' and adjust pin mappings.
"""

config = {
    "interfaces": {
        "LoRa": {
            "type": "LoRaInterface",
            "spi_bus": 0,                    # SPI bus (VSPI on Cardputer)
            "cs_pin": 12,                    # NSS/CS pin
            "reset_pin": 13,                 # RST pin
            "dio0_pin": 14,                  # DIO0 / IRQ pin
            "busy_pin": 15,                  # BUSY pin (SX1262)
            "frequency": 868000000,           # 868 MHz (EU); use 915000000 for US
            "bandwidth": 125000,              # 125 kHz
            "spreadingfactor": 7,             # SF7 = fastest
            "codingrate": 5,                 # 4:5
            "txpower": 14,                    # 14 dBm (25 mW) — Cardputer typical max
            "preamble": 8,                   # 8-symbol preamble
        },
    },

    "transport": {
        # µReticulum stores state in flash with limited writes
        "path": "/flash/rns_state",
    },

    "logging": {
        "loglevel": 3,  # INFO (1=DEBUG, 3=INFO, 5=ERROR)
    },
}
