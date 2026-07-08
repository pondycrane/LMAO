"""Shared helpers for E2E tests (Cardputer flash, LoRa communication).

These helpers are imported by individual E2E test files to avoid code
duplication.  They require pyserial and physical hardware to be connected.
"""

import sys

import serial
import serial.tools.list_ports

# Known USB VID values for RNode-compatible devices.
# Consolidates VID checking into a single set to avoid verbose
# try/except-per-VID anti-pattern.
RNODE_VIDS = {0x303A, 0x10C4, 0x1A86}


def find_rnode_port():
    """Return the device path of a connected Heltec/ESP32 RNode, or *None*.

    RNode devices appear as USB serial (CP210x, CH340, or Espressif USB).
    Also checks for "rnode" in the description string.
    """
    try:
        ports = serial.tools.list_ports.comports()
    except Exception as exc:
        print(f"WARNING: Could not enumerate serial ports: {exc}", file=sys.stderr)
        return None

    for p in ports:
        try:
            if p.vid in RNODE_VIDS:
                return p.device
        except (TypeError, AttributeError):
            pass
        try:
            desc = (p.description or "").lower()
        except (TypeError, AttributeError):
            desc = ""
        if "rnode" in desc:
            return p.device

    return None
