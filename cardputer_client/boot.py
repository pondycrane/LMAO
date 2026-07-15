"""Boot script for M5Stack Cardputer — minimal version."""
import sys
if "/lib" not in sys.path:
    sys.path.insert(0, "/lib")
print("boot OK")