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
    #   DIO3_TCXO: disabled — TCXO init causes Internal radio Status error (7, 3) OpError 0xffff
    #   on this module. The Cap LoRa-1262 may have a TCXO but the driver's TCXO config
    #   sequence puts the chip into an unrecoverable state. Running without TCXO config
    #   works reliably.
    "cardputer_adv": {
        "spi_bus": 2,  # HSPI (SPI3_HOST) — separate from display; SoftSPI is used instead for reliability
        "use_soft_spi": True,  # SoftSPI fallback — HW bus 2 unreliable on some ESP32-S3 builds
        "sck_pin": 40,
        "mosi_pin": 14,
        "miso_pin": 39,
        "cs_pin": 5,
        "busy_pin": 6,
        "dio1_pin": 4,
        "reset_pin": 3,
        "dio2_rf_sw": True,
        # TCXO enabled — Cap LoRa-1262 has a TCXO that needs 1.8V.
        # On SoftSPI (use_soft_spi=True) this works reliably; hardware
        # SPI bus 2 was unreliable with TCXO config.
        "dio3_tcxo_millivolts": 1800,
        "dio3_tcxo_start_time_us": 5000,
        # No battery block — the Cardputer ADV doesn't have a battery ADC.
    },
}
