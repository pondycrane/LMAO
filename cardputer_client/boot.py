"""Boot script for M5Stack Cardputer — runs on MicroPython startup.

MicroPython executes boot.py first, then main.py.
This boot script is intentionally minimal — all application logic
and hardware initialization lives in main.py.
"""

import sys

# Ensure /lib is in the module search path so the urns library
# (uploaded by the flash tool) is importable.
if "/lib" not in sys.path:
    sys.path.insert(0, "/lib")
