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

M5.begin()

# Run the LMAO client
import main
main.main()
