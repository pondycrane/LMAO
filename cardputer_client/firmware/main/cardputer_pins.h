#pragma once

/* M5Stack Cardputer ADV — ESP32-S3 + Cap LoRa-1262 (SX1262) on the rear EXT
 * 2.54-14P header.  Pinout verified from the M5Stack Cardputer ADV schematic
 * and Meshtastic firmware/targets/Cardputer/variant.h; matches the MicroPython
 * preset in cardputer_client/lora_boards.py ("cardputer_adv"). */

/* SX1262 SPI — HSPI (SPI3_HOST), independent of the display SPI.  GPIO39
 * (MTCK) and GPIO40 (MTDO) are JTAG pins reclaimed as regular GPIO at boot. */
#define CARDPUTER_LORA_SCK   40
#define CARDPUTER_LORA_MOSI  14
#define CARDPUTER_LORA_MISO  39
#define CARDPUTER_LORA_NSS   5
#define CARDPUTER_LORA_BUSY  6
#define CARDPUTER_LORA_DIO1  4
#define CARDPUTER_LORA_RST   3
#define CARDPUTER_LORA_SPI_HOST SPI3_HOST

/* Cap LoRa-1262: DIO2 is the RF TX/RX switch; DIO3 feeds a 32 MHz TCXO at
 * 1.8V (RadioLib enables DIO2 as RF switch automatically in begin()). */

/* LMAO mesh radio parameters — MUST match the server's RNode and the client
 * config.py / Sprout DTU (868MHz / BW125 / SF7 / CR 4:5 / preamble 24 /
 * CRC on / syncword 0x1424 / TX 17 dBm).  CRC is forced on after begin() so a
 * real RNode receiver does not drop our frames. */
#define CARDPUTER_LORA_FREQ_MHZ        868.0f
#define CARDPUTER_LORA_BANDWIDTH_KHZ   125.0f
#define CARDPUTER_LORA_SPREADING_FACTOR 7
#define CARDPUTER_LORA_CODING_RATE     5     /* 4:5 */
#define CARDPUTER_LORA_TX_POWER_DBM    17
#define CARDPUTER_LORA_PREAMBLE_LENGTH 24
#define CARDPUTER_LORA_SYNC_WORD       RADIOLIB_SX126X_SYNC_WORD_PRIVATE  /* 0x1424 */
#define CARDPUTER_LORA_TCXO_VOLTAGE    1.8f
