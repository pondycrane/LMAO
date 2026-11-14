/*
 * config.h -- build-time board / network configuration for robodog_client.
 *
 * All values in here are defaults; the network strings below can be overridden
 * at runtime from NVS keys.  The key NAMES have one definition --
 * rd_store.h (RD_NVS_KEY_*) -- shared with the flash scripts, so a rename
 * cannot leave one reader behind.  If a key is absent the #define is used.
 */
#ifndef ROBODOG_CONFIG_H
#define ROBODOG_CONFIG_H

#include "driver/gpio.h"

/* ---- Network config: NVS ONLY, never compiled in.
   flash.sh writes wifi_ssid/wifi_pass/server_host into the 24 KB NVS
   partition at 0x9000 (namespace "robodog" — see rd_store.h) at flash time,
   taking them from --wifi-ssid/--wifi-pass, LMAO_WIFI_SSID/LMAO_WIFI_PASS, or
   the local (gitignored) main/local_secrets.h.  That file is a FLASH-TIME
   source only: it is deliberately NOT included here, so a passphrase can
   never end up inside a built image (it did once, from exactly that include).

   The empty defaults below are what a board with no NVS credentials boots
   with: no WiFi, retried forever, panel shows "no wifi". */
#ifndef RD_WIFI_SSID_DEFAULT
#define RD_WIFI_SSID_DEFAULT ""
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

/* ---- LMAO calendar sync (framed-TCP calendar read on the lmao server) ---- */
/* Hostname, not an IP: tp4's wlan0 is DHCP and its address has already moved
 * once (.67 -> .68), which silently stranded the panel.  The router's DNS
 * resolves the node names, and the ESP32 uses the DHCP-provided resolver. */
#ifndef RD_SERVER_HOST_DEFAULT
#define RD_SERVER_HOST_DEFAULT "tp4"
#endif
#ifndef RD_CAL_TCP_PORT
#define RD_CAL_TCP_PORT        4247u  /* framed-TCP CalendarRequest/Bundle */
#endif
/* Server-side live calendar id; empty request == server default "family". */
#define RD_CALENDAR_ID         "family"

/* ---- Per-board kid assignment (NVS RD_NVS_KEY_KID wins at runtime) ----
   A non-empty value makes this board a kid-dedicated panel: the request
   carries `participant=<kid>` (server filters to that kid's events) and the
   header shows the kid name left + live clock right.  "" = family mode
   (no participant sent, centered datetime header).  Set per board at flash
   time with `flash_firmware --kid-name <kid>`; empty keeps family mode. */
#ifndef RD_KID_NAME_DEFAULT
#define RD_KID_NAME_DEFAULT ""
#endif

/* ---- Timing / robustness ---- */
/* Hold between syncs.  The in-cluster calendar-sync CronJob touches every
 * in-window row hourly (k8s/calendar-sync.yaml), so a 30-min cadence fetched
 * the same reply twice: one sync per hour is the natural floor for both the
 * event delta and the `now` weather slot.  Every sync pays a full WiFi
 * start/assoc/DHCP/DNS, which is the most expensive thing the board does. */
#define RD_SYNC_INTERVAL_SECONDS   3600u
#define RD_WIFI_CONNECT_TIMEOUT_S  15u
/* Hold-loop tick.  The reflective ST7305 keeps its image (the panel is fed
 * from the board rail, not an ESP32 GPIO) and light sleep retains the GPIO
 * state, so the loop sleeps between ticks instead of spinning at 160 MHz.
 * The header clock only moves once a minute, so a tick that lands on an
 * unchanged minute does no render at all. */
#define RD_CLOCK_TICK_MS           30000u
/* CPU frequency band for esp_pm_configure().  80 MHz is ample for a
 * protobuf decode + a u8g2 render + a 24 MHz SPI push; 160 MHz only added
 * heat.  40 MHz (XTAL) is the DFS floor. */
#define RD_PM_MAX_FREQ_MHZ         80u
#define RD_PM_MIN_FREQ_MHZ         40u
/* Separate budgets: connect (non-blocking + select) and the WHOLE reply read
 * (SO_RCVTIMEO per recv plus a total deadline).  One shared value let a
 * slow-drip server stall a full sync cycle. */
#define RD_TCP_CONNECT_TIMEOUT_MS  5000
#define RD_TCP_READ_TIMEOUT_MS     5000
/* Task watchdog: the hold loop resets it every chunk; a wedged sync (or any
 * blocking call that never returns) reboots the board instead of leaving a
 * frozen panel until someone power-cycles it.  Must exceed the worst-case
 * sync (connect + read + render + shot dump) with margin. */
#define RD_WDT_TIMEOUT_S           60u
/* Display clock/calendar timezone: minutes east of UTC.  The esp_sntp
 * component is not in the espressif/idf:v5.3.1 build image, so there is no
 * SNTP: the clock is derived from the server's epoch-ms watermark and advanced
 * by esp_timer (rd_render.c).
 * . -300 = US Eastern Standard (EST, winter) — correct now (DST ended
 *          2026-11-01, the first Sunday of November)
 * . -240 = US Eastern Daylight (EDT, spring/summer)
 * A fixed offset cannot follow DST by itself: flip this value and
 * reflash at each transition (EDT starts the 2nd Sunday of March, EST the
 * 1st Sunday of November).  The server-side `cal_events` times are absolute
 * epoch ms, so only the DISPLAYED clock and the day qualifiers move. */
#ifndef RD_TZ_OFFSET_MINUTES
#define RD_TZ_OFFSET_MINUTES (-300)
#endif
#endif /* ROBODOG_CONFIG_H */
