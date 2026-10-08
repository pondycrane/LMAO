#pragma once

// M5Stack Atom Lite pin map — classic (ESP32) vs Atom Lite S3 (ESP32-S3).
// Selected at build time by the IDF target (idf.py set-target esp32s3) via
// CONFIG_IDF_TARGET_ESP32S3, so one source tree serves both boards.

// Classic Atom Lite (ESP32-PICO, ESP32), the original Sprout / Sprout-Lite:
//   Grove data: moisture ADC1_CH4 / GPIO32, pump GPIO26 ("Grove SDA")
//   front button G39, single SK6812 RGB LED G27 (RMT)
//
// Atom Lite S3 (ESP32-S3), the upgraded Sprout-Lite:
//   Grove (4-pin: 5V GND GPIO1 GPIO2): the two Grove data pins are GPIO1 + GPIO2.
//   Moisture probe -> GPIO1 (ADC1_CH0, an S3 ADC1-capable channel); pump -> GPIO2.
//   Built-in button GPIO41, 4x WS2812 RGB LEDs GPIO35 (RMT).
//   NOTE: which Grove data wire carries the probe vs the pump is a *physical*
//   wiring fact — BOARD_MOISTURE_GPIO/BOARD_PUMP_GPIO default to Grove pin 1 /
//   pin 2 and must be swapped if the harness uses the opposite order.
#include <stdint.h>
#include "driver/adc.h"

#ifdef CONFIG_IDF_TARGET_ESP32S3
#define BOARD_MOISTURE_ADC1_CH   ADC1_CHANNEL_0   // GPIO1
#define BOARD_MOISTURE_GPIO      1
#define BOARD_PUMP_GPIO          2
#define BOARD_BTN_GPIO           41
#define BOARD_LED_GPIO           35
#else
#define BOARD_MOISTURE_ADC1_CH   ADC1_CHANNEL_4   // GPIO32
#define BOARD_MOISTURE_GPIO      32
#define BOARD_PUMP_GPIO          26
#define BOARD_BTN_GPIO           39
#define BOARD_LED_GPIO           27
#endif
