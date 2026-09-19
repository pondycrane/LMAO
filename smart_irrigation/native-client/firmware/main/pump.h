#pragma once

#include <stdbool.h>
#include <stdint.h>

// Watering Unit U101 pump enable — GPIO26 (Grove "SDA"), active-HIGH.
// Verified wiring: docs/hardware-verification.md §2.
#define PUMP_GPIO 26

// 0 = dry run: the control engine runs against the real probe, but the pump is
// NEVER energised.  Flip to 1 only after the #119 gate passes — 10 kOhm
// hardware pull-down fitted AND the OFF level verified through reset/flash with
// the pump disconnected — and only with the rig supervised.
#define PUMP_ACTUATION_ENABLED 0

// Drive the pin to its OFF level.  Must be the first action of app_main;
// idempotent and safe to call on every wake.
bool pump_init(void);

// Energise/de-energise.  The ON edge is gated by PUMP_ACTUATION_ENABLED.  No
// min-on/min-off logic here on purpose: the engine owns the pump timing rules
// (single source of truth), this driver only owns the pin and the accounting.
void pump_set(bool on);

bool pump_is_on(void);

// Cumulative pump on-time since boot (ms).
uint32_t pump_on_ms_total(void);

// Pump on-time since the previous call (ms) — the ML "pump duration" field.
uint32_t pump_take_interval_ms(void);
