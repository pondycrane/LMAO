#pragma once

#include <cstdint>

// Single-wire addressable RGB LED (SK6812 / WS2812) driver for the Atom Lite
// family, shared by every LMAO firmware that has one:
//   * Sprout / Sprout-Lite — G27 (classic) or G35 x4 (S3), status + armed
//   * humidifier Atom Socket — G27, relay ON/OFF indicator
//
// One canonical copy lives here (DRY); each firmware's build.sh stages this
// directory into its ESP-IDF components tree.  The wire protocol is the same
// on every board, so the only per-board inputs are the GPIO, the pixel count
// and an RMT channel.
//
// GRB byte order, 0.1 us RMT ticks: 1 = 0.9 us H + 0.3 us L, 0 = 0.3 us H +
// 0.9 us L, reset >= 80 us.  Legacy RMT TX API (esp_driver_rmt), the one the
// pinned ESP-IDF 5.3 image still ships.
namespace lma_led {

struct Rgb {
    uint8_t r;
    uint8_t g;
    uint8_t b;
};

constexpr Rgb kOff{0, 0, 0};
constexpr Rgb kRed{255, 0, 0};
constexpr Rgb kAmber{255, 96, 0};
constexpr Rgb kGreen{0, 255, 0};

// Configure the pixel GPIO + RMT channel and blank every pixel.  Returns false
// if the RMT driver could not be installed; the caller must treat the LED as
// unavailable and keep running (the appliance control never depends on it).
bool init(int gpio, int pixel_count, int rmt_channel);

// Set every pixel to the same colour.  No-op when init() failed.
void set_all(Rgb color);

}  // namespace lma_led
