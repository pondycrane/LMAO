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

/* build.sh generates this beside the staged main/ — server dest hash, server
 * public key, pinned node identity and the device name.  Include it FIRST so
 * its HF_DEVICE_NAME_DEFAULT wins over the empty fallback below. */
#if __has_include("lmao_config.h")
#include "lmao_config.h"
#endif

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

/* ---- ENV III (SHT30 + QMP6988) on I2C ----
 * The socket's own 4-pin HY2.0 port carries relay-in / VCC_MCU / GND /
 * button-out — it has NO I2C.  The ENV III therefore goes on the Atom Lite's
 * Grove/HY2.0-4P port (yellow = G26, white = G32; Grove I2C is yellow SDA,
 * white SCL) or on the 9-pin header's dedicated I2C pins (G25 = SDA,
 * G21 = SCL).  ESP32's I2C pins are fully routable, so the driver probes both
 * pairs (and both polarities) and keeps the first where a CRC-valid SHT30
 * answers; the boot log names the pair that worked. */
#define HF_ENV_PAIR_COUNT 4
#define HF_ENV_SDA_G26 26
#define HF_ENV_SCL_G32 32
#define HF_ENV_SDA_G25 25
#define HF_ENV_SCL_G21 21

/* ---- reporting to the LMAO server ---- */
#ifndef HF_SERVER_HOST_DEFAULT
#define HF_SERVER_HOST_DEFAULT "tp4"
#endif
#ifndef HF_SERVER_PORT_DEFAULT
#define HF_SERVER_PORT_DEFAULT 4246
#endif
/* SensorReport.node_id + announce display name.  Overridden by the generated
 * lmao_config.h when the build is provisioned with a device name. */
#ifndef HF_DEVICE_NAME_DEFAULT
#define HF_DEVICE_NAME_DEFAULT ""
#endif
#ifndef HF_SENSOR_INTERVAL_S
#define HF_SENSOR_INTERVAL_S 30
#endif
#ifndef HF_REPORT_INTERVAL_S
#define HF_REPORT_INTERVAL_S 60
#endif
#ifndef HF_ANNOUNCE_INTERVAL_MS
#define HF_ANNOUNCE_INTERVAL_MS 30000
#endif

/* ---- directive pull ----
 * The socket polls the LMAO server's directive relay from its own outbound
 * path (the one its SensorReport uses).  Nothing pushes to the socket, so
 * actuation does not depend on anyone resolving its DHCP address.
 * The key is the NVS mdns_host; the relay is the server host + this port. */
#ifndef HF_DIRECTIVE_PORT
#define HF_DIRECTIVE_PORT 8082
#endif
#ifndef HF_DIRECTIVE_POLL_S
#define HF_DIRECTIVE_POLL_S 30
#endif

/* Local interlocks: the last line of defence if the controller or its law
 * misbehaves.  Sized like the controller's own caps (3 h run, 10 min
 * cooldown); the lease is still the deadman. */
#ifndef HF_LOCAL_MAX_ON_S
#define HF_LOCAL_MAX_ON_S (3 * 3600)
#endif
#ifndef HF_LOCAL_COOLDOWN_S
#define HF_LOCAL_COOLDOWN_S 600
#endif

/* ---- loop cadence ---- */
#define HF_TICK_MS 200
#define HF_METER_STALE_MS 5000
/* How long a silent meter may stay silent before the boot probe switches the
 * UART framing (8E1 -> 8N1 -> inverted). */
#define HF_METER_PROBE_MS 8000

#endif
