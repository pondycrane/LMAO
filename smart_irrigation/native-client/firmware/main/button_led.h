#pragma once

#include <stdbool.h>

// Atom Lite front control: the G39 button + the single SK6812 RGB LED on G27.
// On every boot the node is in DRY RUN; the button is the runtime actuation
// toggle:
//   * hold ~2 s  -> arm actuation (red LED on)
//   * quick tap  -> back to dry-run (LED off)
// The armed state is deliberately NOT persisted (see pump.h): a reset always
// drops back to dry-run.  This module only produces the arm decision + LED; the
// caller owns applying it to the pump (pump_set_actuation) and logging.

// Spawn the button/LED task, LED off (dry-run).  Call once after pump_init();
// g_actuation defaults off so the node always boots dry-run.
void button_led_init(void);

// Current armed state (volatile bool, safe to read).
bool button_led_armed(void);

// Returns true once after the armed state changed since the last call; the
// caller applies it to the pump.  Non-blocking.
bool button_led_take_pending_change(void);
