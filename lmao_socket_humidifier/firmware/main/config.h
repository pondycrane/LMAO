/*
 * config.h -- Atom Socket board wiring + build-time defaults.
 *
 * Hardware (M5Stack Atom Socket K055, Atom Lite / ESP32-PICO-D4 inside):
 *   relay      -> GPIO23, active HIGH (relay closed = mains on)
 *   HLW8032 RX -> GPIO22, UART2 @ 4800 8E1 (metering chip transmits only)
 *   RGB LED    -> GPIO27, single SK6812 (relay ON/OFF indicator)
 *
 * The factory firmware's pin facts come from the M5Stack driver
 * (M5Atom/examples/ATOM_BASE/ATOM_Socket/AtomSocket.cpp) and the product
 * PinMap; ESPHome's atom-socket.factory.yaml agrees (uart rx GPIO22,
 * switch GPIO23).
 *
 * Build-time overrides are generated into lmao_config.h by build.sh.  WiFi
 * credentials are NEVER compiled in: flash.sh writes them to the NVS
 * partition at flash time (namespace "humidifier").
 */
#ifndef HF_CONFIG_H
#define HF_CONFIG_H

#include "driver/gpio.h"

/* ---- relay + metering pins ---- */
#define HF_RELAY_PIN GPIO_NUM_23
#define HF_METER_UART UART_NUM_2
#define HF_METER_RX_PIN GPIO_NUM_22
#define HF_METER_BAUD 4800

/* ---- relay indicator ----
 * One SK6812 pixel on the Atom Lite's GPIO27.  Driven from relay_write(), so
 * the LED is a hardware-visible copy of the GPIO state rather than a guess:
 * lit red = relay closed (mains live), dark = relay open.  The factory sketch
 * uses the same pin and the same red/amber semantics. */
#define HF_LED_PIN GPIO_NUM_27
#define HF_LED_PIXELS 1
#define HF_LED_RMT_CHANNEL 0

/* ---- HTTP control API ---- */
#ifndef HF_HTTP_PORT
#define HF_HTTP_PORT 80
#endif

/* ---- relay lease (deadman) ----
 * Every ON request carries a lease; when the controller stops renewing, the
 * relay opens on its own.  A humidifier left running by a dead controller is
 * the failure mode this exists to prevent, so the default is short. */
#ifndef HF_DEFAULT_LEASE_S
#define HF_DEFAULT_LEASE_S 300
#endif
#ifndef HF_MAX_LEASE_S
#define HF_MAX_LEASE_S 3600
#endif

/* ---- WiFi / mDNS ---- */
#ifndef HF_WIFI_CONNECT_TIMEOUT_S
#define HF_WIFI_CONNECT_TIMEOUT_S 30
#endif
#ifndef HF_MDNS_HOST_DEFAULT
#define HF_MDNS_HOST_DEFAULT "humidifier"
#endif

/* ---- loop cadence ---- */
#define HF_TICK_MS 200
#define HF_METER_STALE_MS 5000
/* How long a silent meter may stay silent before the boot probe switches the
 * UART framing (8E1 -> 8N1 -> inverted). */
#define HF_METER_PROBE_MS 8000

#endif
