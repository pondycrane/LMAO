"""
Smart-irrigation hardware probe — run on the Atom Lite (MicroPython).

Usage (host machine):
    mpremote connect <port> run smart_irrigation/firmware/tools/probe_hardware.py

Prints ``PROBE ...`` result lines for machine parsing and a human summary.

SAFETY
------
- The watering-module pump control pin is NEVER configured, read, or driven.
  Keep the pump disconnected while this probe runs.
- The DTU receives READ-ONLY AT queries only (no join, no send, no config
  writes, no reboot).
- The I2C mux channel register is restored to 0 (all channels off) on exit.
- No esptool, no flashing, no filesystem writes.

Verified pin map (2026-09-12 hardware verification):
  - I2C  (Grove port) : SCL=G32, SDA=G26       (mux PCA9548A @ 0x70)
  - DTU  (RAK3172)    : TX=G22, RX=G19 @ 115200 (RUI_4.0.6_RAK3172-E)
"""

import sys
import time

from machine import I2C, Pin, UART

# ---------- verified pin map (see smart_irrigation/docs/hardware-verification.md)
I2C_SCL = 32
I2C_SDA = 26
DTU_TX = 22
DTU_RX = 19
DTU_BAUD = 115200

MUX_ADDRS = tuple(range(0x70, 0x78))
SHT30_ADDR = 0x44
SHT30_CMD = b"\x2c\x06"  # high repeatability, clock stretching disabled
QMP_ID_REG = 0xD1  # QMP6988 chip-ID register (value 0x5C)


def emit(*parts):
    print("PROBE", *parts)


def _safe(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) returning (ok, value); never raises."""
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:  # MicroPython: broad catch is intended here
        return False, exc


def device_info():
    import os
    import machine

    uname = os.uname()
    info = {
        "platform": sys.platform,
        "version": uname.release,
        "machine": uname.machine,
        "freq_hz": machine.freq(),
    }
    ok, flash = _safe(__import__("esp").flash_size)
    if ok:
        info["flash_bytes"] = flash
    ok, resets = _safe(machine.reset_cause)
    if ok:
        info["reset_cause"] = resets
    try:
        import network

        sta = network.WLAN(network.STA_IF)
        info["mac"] = ":".join("%02x" % b for b in sta.config("mac"))
    except Exception:
        pass
    emit("DEVICE", " ".join("%s=%s" % (k, v) for k, v in info.items()))
    return info


def scan_bus(i2c):
    ok, found = _safe(i2c.scan)
    if not ok:
        emit("I2C", "scl=%d sda=%d scan_error=%s" % (I2C_SCL, I2C_SDA, found))
        return []
    emit(
        "I2C",
        "scl=%d sda=%d devices=%s"
        % (I2C_SCL, I2C_SDA, ",".join("0x%02x" % a for a in found) or "none"),
    )
    return found


def find_mux(found):
    for addr in found:
        if addr in MUX_ADDRS:
            return addr
    return None


def mux_select(i2c, mux, channel):
    i2c.writeto(mux, bytes([1 << channel]))
    time.sleep_ms(30)


def mux_off(i2c, mux):
    try:
        i2c.writeto(mux, bytes([0x00]))
    except Exception:
        pass


def read_sht30(i2c, addr=SHT30_ADDR):
    i2c.writeto(addr, SHT30_CMD)
    time.sleep_ms(50)
    data = i2c.readfrom(addr, 6)
    temp = -45.0 + 175.0 * ((data[0] << 8) | data[1]) / 65535.0
    hum = 100.0 * ((data[3] << 8) | data[4]) / 65535.0
    return round(temp, 2), round(hum, 2)


def probe_mux_channels(i2c, mux):
    """Scan each mux channel; return {channel: [devices_without_mux]}."""
    per_channel = {}
    for channel in range(8):
        mux_select(i2c, mux, channel)
        ok, found = _safe(i2c.scan)
        if not ok:
            found = []
        devices = [a for a in found if a != mux]
        per_channel[channel] = devices

        # QMP6988 collision signature: a chip-ID read at 0x70 on a channel
        # where a QMP6988 (also 0x70) lives returns AND(mux_echo, 0x5C)
        # instead of the mux echo (0xD1).  See hardware-verification.md.
        collision = "-"
        ok, val = _safe(i2c.readfrom_mem, mux, QMP_ID_REG, 1)
        if ok and val != b"\xd1":
            collision = "QMP_COLLISION(read=0x%02x)" % val[0]

        emit(
            "MUX_CH",
            "ch=%d devices=%s %s"
            % (
                channel,
                ",".join("0x%02x" % a for a in devices) or "none",
                collision,
            ),
        )
    mux_off(i2c, mux)
    return per_channel


def probe_dtu():
    uart = UART(
        2,
        baudrate=DTU_BAUD,
        tx=Pin(DTU_TX),
        rx=Pin(DTU_RX),
        timeout=600,
        rxbuf=1024,
    )
    time.sleep_ms(300)
    while uart.any():
        uart.read()

    def at(cmd, wait=0.5):
        uart.write((cmd + "\r\n").encode())
        time.sleep(wait)
        data = b""
        deadline = time.ticks_add(time.ticks_ms(), 1800)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            chunk = uart.read()
            if chunk:
                data += chunk
                deadline = time.ticks_add(time.ticks_ms(), 600)
        return data

    raw = at("AT")
    alive = b"OK" in raw
    ver = hwmodel = nwm = ""
    if alive:
        r = at("AT+VER=?")
        ver = _at_value(r, "AT+VER=")
        r = at("AT+HWMODEL=?")
        hwmodel = _at_value(r, "AT+HWMODEL=")
        r = at("AT+NWM=?")
        nwm = _at_value(r, "AT+NWM=")
    emit(
        "DTU",
        "tx=%d rx=%d baud=%d alive=%s ver=%s hwmodel=%s nwm=%s"
        % (DTU_TX, DTU_RX, DTU_BAUD, alive, ver or "-", hwmodel or "-", nwm or "-"),
    )
    return alive


def _at_value(response, prefix):
    if not response:
        return ""
    text = response.decode("ascii", "ignore") if hasattr(response, "decode") else str(response)
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def main():
    emit("START", "smart-irrigation hw probe")
    device_info()

    ok, i2c = _safe(I2C, 0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=100000)
    if not ok:
        emit("I2C", "init_error=%s" % i2c)
        return
    found = scan_bus(i2c)

    mux = find_mux(found)
    per_channel = {}
    if mux is not None:
        emit("MUX", "addr=0x%02x" % mux)
        per_channel = probe_mux_channels(i2c, mux)
    else:
        emit("MUX", "not_found (expected 0x70-0x77)")

    # SHT30 measurement, on whichever channel it was found
    sht_channel = None
    for channel, devices in per_channel.items():
        if SHT30_ADDR in devices:
            sht_channel = channel
            break
    if sht_channel is not None and mux is not None:
        mux_select(i2c, mux, sht_channel)
        ok, val = _safe(read_sht30, i2c)
        if ok:
            emit(
                "SHT30",
                "ch=%d addr=0x%02x temp_c=%s humidity_pct=%s"
                % (sht_channel, SHT30_ADDR, val[0], val[1]),
            )
        else:
            emit("SHT30", "ch=%d read_error=%s" % (sht_channel, val))
        mux_off(i2c, mux)
    else:
        emit("SHT30", "not_found")

    probe_dtu()
    emit("DONE", "note=pressure/QMP requires the address-collision fix")


main()
