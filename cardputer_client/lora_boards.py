"""
Cardputer LoRa board pinout preset for µReticulum.

The M5Stack Cardputer ADV has an onboard SX1262 LoRa radio connected via SPI.
This preset is referenced by name from config.py.
"""

LORA_BOARDS = {
    # M5Stack Cardputer ADV — onboard SX1262 LoRa radio (Cap LoRa-1262 module)
    # Pinout verified from M5Stack Cardputer ADV schematic and Meshtastic
    # firmware variant.h (firmware/targets/Cardputer/variant.h):
    #   SPI bus: 2 (HSPI / SPI3_HOST) — independent from display SPI
    #   SCK:  GPIO 40  (MTDO — JTAG, must be reclaimed for GPIO)
    #   MOSI: GPIO 14
    #   MISO: GPIO 39  (MTCK — JTAG, must be reclaimed for GPIO)
    #   CS:   GPIO 5
    #   RST:  GPIO 3
    #   BUSY: GPIO 6
    #   DIO1: GPIO 4  (IRQ)
    #   DIO2_RF_SW: True  (antenna switch on DIO2)
    #   DIO3_TCXO: 1800 mV (TCXO voltage, 1.8V)
    #   TCXO startup: 5000 us (some modules need longer than 1000us default)
    "cardputer_adv": {
        "spi_bus": 2,  # HSPI (SPI3_HOST) — separate from display
        "sck_pin": 40,
        "mosi_pin": 14,
        "miso_pin": 39,
        "cs_pin": 5,
        "busy_pin": 6,
        "dio1_pin": 4,
        "reset_pin": 3,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
        "dio3_tcxo_start_time_us": 5000,
        # No battery block — the Cardputer ADV doesn't have a battery ADC.
    },
}
