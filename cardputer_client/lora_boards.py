"""
Cardputer LoRa board pinout preset for µReticulum.

The M5Stack Cardputer ADV has an onboard SX1262 LoRa radio connected via SPI.
This preset is referenced by name from config.py.
"""

LORA_BOARDS = {
    # M5Stack Cardputer ADV — onboard SX1262 LoRa radio
    # Pin mappings from the M5Stack Cardputer ADV schematic:
    #   SPI bus: 1 (VSPI on ESP32-S3)
    #   SCK:  GPIO 36  (shared with display)
    #   MOSI: GPIO 35  (shared with display)
    #   MISO: GPIO 37  (shared with display)
    #   CS:   GPIO 12
    #   RST:  GPIO 13
    #   BUSY: GPIO 15  (DIO3 / busy pin for SX1262)
    #   DIO1: GPIO 14  (IRQ)
    "cardputer_adv": {
        "spi_bus": 2,  # HSPI (display uses SPI2_HOST too, LoRa separate device)
        "sck_pin": 40,
        "mosi_pin": 14,
        "miso_pin": 39,
        "cs_pin": 5,
        "busy_pin": 6,
        "dio1_pin": 4,
        "reset_pin": 3,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
        # No battery block — the Cardputer ADV doesn't have a battery ADC.
    },
}
