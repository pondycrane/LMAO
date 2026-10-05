#!/usr/bin/env python3
"""Identify LMAO USB devices WITHOUT a MAC table — by chip type / USB descriptor
(Cardputer) + attached-peripheral evidence (DTU/RAK LoRa on UART2 + ENV III on
I2C, both Sprout-only).

Non-disruptive: reads USB descriptors / device-by-id (no reset) + samples the
serial console for the running firmware's evidence.

Classification:
  - Espressif USB-JTAG / "Cardputer" device      -> Cardputer   (ESP32-S3, chip type)
  - M5Stack + console shows RAK-LoRa / ENV III    -> Sprout      (DTU + ENV III)
  - M5Stack + console shows moisture/pump only    -> Sprout-LITE (water unit only)
  - M5Stack + console unreadable                  -> UNREADABLE — replug or pin-probe

Why console evidence: UART2 (RAK) + I2C (ENV III) are ESP GPIO, not the USB
console path, so the host cannot probe them via USB directly. The native
firmware only drives the RAK (AT on UART2) and ENV III (I2C) when those
peripherals are attached; the Sprout-LITE firmware compiles the radio + ENV III
out (#if SPROUT_LITE) and logs only moisture/pump.
"""
from __future__ import annotations

import argparse
import glob
import os
import time

import serial
import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

_RADIO_MARKERS = (
    "uart_at", "announced", "on-mesh", "path-request", "at+pfreq", "via at+psend",
)
_ENV_MARKERS = ("sensorreport env", "air ", "temp/hum")
_MOISTURE_MARKERS = ("moisture", "pump_cmd", "probe=ok")


def _product_string(p: ListPortInfo) -> str:
    return " ".join(str(x) for x in (getattr(p, "product", None), p.description) if x).lower()


def is_cardputer(p: ListPortInfo) -> bool:
    ps = _product_string(p)
    return "cardputer" in ps or "jtag" in ps


def console_sample(port: str, seconds: int = 45) -> str:
    """Read up to `seconds` of console output, retrying the flaky M5Stack bridge."""
    blob = b""
    deadline = time.time() + seconds
    attempts = 0
    while time.time() < deadline and attempts < 6:
        attempts += 1
        try:
            s = serial.Serial(port, 115200, timeout=0.6)
            s.reset_input_buffer()
            end = time.time() + max(min((deadline - time.time()), 20), 4)
            while time.time() < end:
                try:
                    d = s.read(8192)
                    if d:
                        blob += d
                except serial.SerialException:
                    break
            s.close()
            if blob:
                break
        except Exception:
            time.sleep(1)
    return blob.decode("utf-8", "replace")


def classify(port: str, blob: str) -> str:
    text = blob.lower()
    radio = any(m in text for m in _RADIO_MARKERS)
    env = any(m in text for m in _ENV_MARKERS)
    moist = any(m in text for m in _MOISTURE_MARKERS)
    if radio and env:
        return "Sprout (RAK LoRa + ENV III)"
    if radio:
        return "Sprout (RAK LoRa; ENV III not seen)"
    if env:
        return "Sprout (ENV III; radio not seen)"
    if moist:
        return "Sprout-LITE (moisture-only)"
    if not blob.strip():
        return "CONSOLE UNREADABLE (USB wedged) — replug, or pin-probe UART2 (RAK AT) / I2C (ENV III)"
    return "Unknown (no radio/ENV/moisture evidence in console)"


def find_cardputers() -> list[str]:
    """The Cardputer's Espressif USB-JTAG is often absent from list_ports;
    discover any /dev/ttyACM* and match it via /dev/serial/by-id."""
    out = []
    for port in sorted(glob.glob("/dev/ttyACM*")):
        try:
            real = os.path.realpath(port)
            matched = False
            for lnk in glob.glob("/dev/serial/by-id/*"):
                if os.path.realpath(lnk) == real:
                    nm = os.path.basename(lnk).lower()
                    if "cardputer" in nm or "jtag" in nm:
                        out.append(port)
                        matched = True
                    break
            if not matched:
                out.append(port)  # a ttyACM here is the S3 USB-JTAG -> Cardputer
        except Exception:
            out.append(port)
    return out


def identify_all() -> None:
    header = f"{'PORT':<12} TYPE"
    print(header)
    print("-" * 78)

    cardputers = set(find_cardputers())
    seen: set[str] = set()
    for c in sorted(cardputers):
        seen.add(c)
        print(f"{c:<12} Cardputer (ESP32-S3 / USB-JTAG)", flush=True)

    for p in serial.tools.list_ports.comports():
        if p.device in seen:
            continue
        ps = _product_string(p)
        if not ("m5stack" in ps or "cardputer" in ps):
            continue  # not a LMAO USB device (e.g. Pi /dev/ttyAMA0 PL011)
        blob = console_sample(p.device)
        print(f"{p.device:<12} {classify(p.device, blob)}", flush=True)


def identify_single(port: str) -> str:
    """Return one machine-readable verdict line for a single port.

    Cheap + non-disruptive: descriptor/Cardputer check first, then a bounded
    console sample for M5Stack-family boards. Raises ValueError if the port is
    not a LMAO device.
    """
    for p in serial.tools.list_ports.comports():
        if p.device != port:
            continue
        if is_cardputer(p):
            return "Cardputer (ESP32-S3 / USB-JTAG)"
        ps = _product_string(p)
        if "m5stack" in ps:
            return classify(port, console_sample(port))
        raise ValueError(f"{port} is not a LMAO device (got '{ps}')")
    if port in find_cardputers():
        return "Cardputer (ESP32-S3 / USB-JTAG)"
    raise ValueError(f"{port}: no LMAO USB serial device found")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", metavar="X",
                    help="identify a single port; omit to classify all devices")
    a = ap.parse_args()
    if a.port:
        try:
            print(identify_single(a.port))
        except ValueError as e:
            print(f"UNKNOWN ({e})")
    else:
        identify_all()


if __name__ == "__main__":
    main()
