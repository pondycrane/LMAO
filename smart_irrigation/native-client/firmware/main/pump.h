#pragma once

#include <stdbool.h>
#include <stdint.h>

// Watering Unit U101 pump enable — GPIO26 (Grove "SDA"), active-HIGH.
// Verified wiring: docs/hardware-verification.md §2.
#define PUMP_GPIO 26

// Runtime actuation gate (was a compile-time #define).  Every boot starts in
// DRY RUN: pump_set(true) is refused until the user arms actuation — on the
// Atom Lite that is a deliberate 2 s hold on the G39 button (red LED = armed),
// see button_led.h.  The armed state is deliberately NOT persisted: a reset or
// power cycle always drops back to dry-run for safety.  Never rely on it until
// the #119 gate passes — 10 kOhm hardware pull-down fitted AND the OFF level
// verified through reset/flash with the pump disconnected.
void pump_set_actuation(bool enabled);
bool pump_actuation_enabled(void);

// Drive the pin to its OFF level.  Must be the first action of app_main;
// idempotent and safe to call on every wake.
bool pump_init(void);

// Energise/de-energise.  The ON edge is gated by the runtime actuation flag
// (pump_actuation_enabled).  No min-on/min-off logic here on purpose: the
// engine owns the pump timing rules (single source of truth), this driver only
// owns the pin and the accounting.
void pump_set(bool on);

bool pump_is_on(void);

// Cumulative pump on-time since boot (ms).
uint32_t pump_on_ms_total(void);

// Pump on-time since the previous call (ms) — the ML "pump duration" field.
uint32_t pump_take_interval_ms(void);
