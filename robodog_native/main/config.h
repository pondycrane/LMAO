/*
 * config.h -- build-time board / network configuration for robodog_native.
 *
 * All values in here are defaults; the three network strings below can be
 * overridden at runtime from NVS keys:
 *   "wifi_ssid" (string), "wifi_pass" (string), "server_host" (string)
 * If a key is absent the corresponding #define is used.
 */
#ifndef ROBODOG_CONFIG_H
#define ROBODOG_CONFIG_H

#include "driver/gpio.h"

/* ---- Optional per-build secrets (WiFi SSID/pass, server host) — never commit.
   Create robodog_native/main/local_secrets.h with:
       #define RD_WIFI_SSID_DEFAULT     "MySSID"
       #define RD_WIFI_PASS_DEFAULT     "secret"
       #define RD_SERVER_HOST_DEFAULT   "192.168.10.19"
   It is gitignored; the value-settings below are guarded so the file may
   pre-set them.  NVS keys ("wifi_ssid"/"wifi_pass"/"server_host") still win
   over all of these at runtime. */
#if __has_include("local_secrets.h")
#include "local_secrets.h"
#endif

/* ---- ST7305 RLCD-4.2 SPI pinout (Waveshare ESP32-S3 board) ---- */
#define RD_MOSI_PIN GPIO_NUM_12
#define RD_SCK_PIN  GPIO_NUM_11
#define RD_DC_PIN   GPIO_NUM_5
#define RD_CS_PIN   GPIO_NUM_40
#define RD_RST_PIN  GPIO_NUM_41
#define RD_TE_PIN   GPIO_NUM_6 /* tearing-effect line; not used by the driver */

/* ---- WiFi (bake here, or override via NVS keys above) ---- */
#ifndef RD_WIFI_SSID_DEFAULT
#define RD_WIFI_SSID_DEFAULT ""
#endif
#ifndef RD_WIFI_PASS_DEFAULT
#define RD_WIFI_PASS_DEFAULT ""
#endif

/* ---- LMAO calendar TCP service (lma_core/calendar_tcp_api.py) ---- */
#ifndef RD_SERVER_HOST_DEFAULT
#define RD_SERVER_HOST_DEFAULT "192.168.50.67"
#endif
#define RD_CAL_TCP_PORT        4247u
/* Server-side live calendar id; empty request == server default "family". */
#define RD_CALENDAR_ID         "family"

/* ---- SNTP (best-effort; the esp_sntp component is not in the espressif/idf
   :v5.3.1 build image, so it is off — guarded by RD_SNTP_ENABLED). ---- */
#define RD_SNTP_ENABLED       0
#ifndef RD_SNTP_SERVER
#define RD_SNTP_SERVER        "pool.ntp.org"
#endif

/* ---- Timing / robustness ---- */
#define RD_SYNC_INTERVAL_SECONDS   1800u /* light-sleep between syncs */
#define RD_WIFI_CONNECT_TIMEOUT_S  15u
#define RD_TCP_CONNECT_TIMEOUT_MS  5000
#define RD_TCP_FRAME_TIMEOUT_MS    5000
#define RD_SNTP_WAIT_MAX_MS        3000u

#endif /* ROBODOG_CONFIG_H */
