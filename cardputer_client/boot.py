"""
Boot script for M5Stack Cardputer ADV — LMAO client.

Initializes M5Stack hardware, sets up the library path, then runs
the LMAO µReticulum client (main.py).
"""
import sys
import M5

if "/flash/lib" not in sys.path:
    sys.path.insert(0, "/flash/lib")
if "/flash" not in sys.path:
    sys.path.insert(0, "/flash")

M5.begin()

# Run the LMAO client
import main
main.main()
