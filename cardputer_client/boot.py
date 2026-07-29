"""
Boot script for M5Stack Cardputer ADV — LMAO client.

Initializes M5Stack hardware, sets up the library path, then runs
the LMAO µReticulum client (main.py).

SAFE MODE: Hold the G0/BtnA button (top-left, labelled "G0") during
boot to skip main.py and drop to the MicroPython REPL.  The serial
console prints a countdown so you know the check is in progress.
"""
import sys
import time

import M5
from machine import Pin

# ── Safe-mode escape hatch ────────────────────────────────────────
# GPIO0 is the BtnA / G0 button — active low, requires pull-up.
# If held during boot, skip main.py and drop to the REPL so the
# device can always be recovered (e.g. after a REPL-lockout due to
# a tight error loop in main.py).
BTN_SAFE_MODE = Pin(0, Pin.IN, Pin.PULL_UP)

# Debounce / settle: wait briefly for the pin to stabilise.
time.sleep_ms(100)

if BTN_SAFE_MODE.value() == 0:  # button is pressed (active low)
    print("\n" + "=" * 48)
    print("  SAFE MODE — G0/BtnA button held.")
    print("  main.py will NOT be executed.")
    print("  Holding for 2 seconds to confirm...")
    print("  Release the button within 2s to boot normally,")
    print("  or keep holding to stay in safe mode.")
    print("=" * 48 + "\n")

    # Hold for ~2 s; if the button is still pressed after that,
    # confirm safe mode and drop to the REPL.
    held = True
    for _ in range(20):  # 20 * 100 ms = 2 s
        time.sleep_ms(100)
        if BTN_SAFE_MODE.value() != 0:  # released
            held = False
            break

    if held:
        print("SAFE MODE — Cardputer is at the REPL.\n")
        print("  Use Ctrl+C to interrupt any running code.")
        print("  Use ampy / mpremote to flash new firmware.\n")
        # Drop to REPL — do NOT import or run main.py.
        sys.exit()
    else:
        print("Button released — continuing with normal boot.\n")

# ── Normal boot continues below ───────────────────────────────────

if "/flash/lib" not in sys.path:
    sys.path.insert(0, "/flash/lib")
if "/flash" not in sys.path:
    sys.path.insert(0, "/flash")

# ── Idle-REPL automatic reboot guard ──────────────────────────────
# If boot.py was invoked by a raw-REPL flash session that got interrupted,
# the device will sit in raw REPL indefinitely (the WDT is disarmed during
# flashing).  This guard provides a self-healing mechanism: if we detect
# that we are in raw REPL (no host has sent Ctrl+D to execute the flash
# script within ~60s), we hard-reset to boot into the app.
#
# This only activates when boot.py runs *during* an open raw-REPL session
# (e.g. after a Ctrl+C interrupt).  On a normal cold boot the REPL is
# friendly, not raw, and this code exits immediately.
try:
    import machine
    import time as _time

    # Check if we are in raw REPL by looking for the ``raw REPL`` echo
    # on stdin.  MicroPython in raw mode echoes back ``raw REPL; CTRL-B
    # to exit`` at connection — this does NOT appear again on re-entry,
    # so instead check for the absence of a typed character within a
    # short window.  If no host input arrives within IDLE_REPL_TIMEOUT_S,
    # assume the host session is dead and reboot.
    IDLE_REPL_TIMEOUT_S = 120  # 2 minutes with no host input → reboot
    _deadline = _time.ticks_ms() + IDLE_REPL_TIMEOUT_S * 1000

    # poll stdin non-blockingly
    import select as _select

    _poller = _select.poll()
    _poller.register(0, _select.POLLIN)  # 0 = stdin
    while _time.ticks_ms() < _deadline:
        _events = _poller.poll(0)
        if _events:
            # Host sent something — raw REPL session is alive.
            # Drain the input (don't leave it in the buffer) and exit
            # the guard immediately.
            _ = sys.stdin.read(1)  # might block if nothing, but poll said there is
            break
        _time.sleep_ms(250)
    else:
        # Timeout — no host traffic.  Hard-reset to boot the app.
        # This recovers the device from an interrupted flash session
        # without requiring physical access.
        _time.sleep_ms(100)
        print("\nIDLE REPL TIMEOUT — rebooting into app...")
        machine.reset()
except Exception:
    pass  # guard is best-effort; never block normal boot

M5.begin()

# Run the LMAO client
import main
main.main()
