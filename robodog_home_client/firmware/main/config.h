/*
 * config.h -- board / network defaults for the RoboDog home client.
 *
 * The Waveshare ESP32-S3 RLCD-4.2 board carries an ST7305 reflective panel
 * (SPI) and an SHTC3 temperature/humidity sensor on I2C (SDA=13, SCL=14).
 *
 * Build-time overrides live in the generated lmao_config.h (build.sh):
 *   RH_WIFI_SSID_DEFAULT / RH_WIFI_PASS_DEFAULT
 *   RH_SERVER_HOST_DEFAULT / RH_SERVER_PORT_DEFAULT
 *   RH_INTERVAL_SECONDS
 *   RH_DEVICE_NAME_DEFAULT
 *   RH_NODE_IDENTITY_HEX / RH_DEST_HASH_HEX
 * Runtime NVS keys (namespace "robodog_home") still win for the strings.
 */
#ifndef RH_CONFIG_H
#define RH_CONFIG_H

#include "driver/gpio.h"

#if __has_include("lmao_config.h")
#include "lmao_config.h"
#endif

/* ---- ST7305 RLCD-4.2 SPI pinout (same board wiring as robodog_client) ---- */
#define RH_MOSI_PIN GPIO_NUM_12
#define RH_SCK_PIN  GPIO_NUM_11
#define RH_DC_PIN   GPIO_NUM_5
#define RH_CS_PIN   GPIO_NUM_40
#define RH_RST_PIN  GPIO_NUM_41
#define RH_TE_PIN   GPIO_NUM_6 /* tearing-effect line; unused */

/* ---- SHTC3 I2C (Waveshare RLCD-4.2: SDA=13, SCL=14, addr 0x70) ---- */
#define RH_I2C_PORT     I2C_NUM_0
#define RH_I2C_SDA_PIN  GPIO_NUM_13
#define RH_I2C_SCL_PIN  GPIO_NUM_14
#define RH_I2C_FREQ_HZ  100000
#define RH_SHTC3_ADDR   0x70

/* ---- WiFi (baked at build time; NVS keys override at runtime) ---- */
#ifndef RH_WIFI_SSID_DEFAULT
#define RH_WIFI_SSID_DEFAULT ""
#endif
#ifndef RH_WIFI_PASS_DEFAULT
#define RH_WIFI_PASS_DEFAULT ""
#endif

/* ---- LMAO server Reticulum TCP interface ---- */
#ifndef RH_SERVER_HOST_DEFAULT
#define RH_SERVER_HOST_DEFAULT "tp4"
#endif
#ifndef RH_SERVER_PORT_DEFAULT
#define RH_SERVER_PORT_DEFAULT 4246
#endif

/* ---- Device name (contact book / announce display name) ---- */
#ifndef RH_DEVICE_NAME_DEFAULT
#define RH_DEVICE_NAME_DEFAULT ""
#endif

/* ---- Reporting cadence ---- */
#ifndef RH_INTERVAL_SECONDS
#define RH_INTERVAL_SECONDS 60u
#endif
#ifndef RH_ANNOUNCE_INTERVAL_MS
#define RH_ANNOUNCE_INTERVAL_MS 30000u
#endif
#ifndef RH_TICK_MS
#define RH_TICK_MS 100u
#endif

/* ---- Chart history depth (matches the server's env ring) ---- */
#define RH_HISTORY_MAX 30

/* ---- Display timezone for the chart's x-axis clock ---- */
/* Minutes east of UTC.  There is no tz database on the device: flip this at
 * each DST transition and reflash.
 *   -300 = US Eastern Standard (EST, winter; DST ended 2026-11-01)
 *   -240 = US Eastern Daylight (EDT, spring/summer) */
#ifndef RH_TZ_OFFSET_MINUTES
#define RH_TZ_OFFSET_MINUTES (-300)
#endif

/* ---- Timing / robustness ---- */
#define RH_WIFI_CONNECT_TIMEOUT_S 30u
#define RH_WDT_TIMEOUT_S          120u

#endif /* RH_CONFIG_H */
