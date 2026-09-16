"""E2E test for the Sprout smart irrigation node.

Sprout = M5Stack Atom Lite stacked on the DTU LoRaWAN base (A152-EU868) with
the Watering Unit U101 (moisture + pump) on the Atom Grove port and the ENV III
board on the base's Grove Port A.

Requires the Sprout rig connected to this machine's USB (FTDI ``0403:6001``
"M5stack", serial ``69526EE94F``).  The test auto-skips when Sprout is not
detected.

Verified pin map (smart_irrigation/docs/hardware-verification.md, 2026-09-16):
  * moisture  : analog ADC on GPIO32 (Grove "SCL")
  * pump      : GPIO26 (Grove "SDA"), active-HIGH -- READ ONLY, never driven
  * ENV III   : base Port A I2C bus SCL=G21 / SDA=G25 (SHT30 @ 0x44, QMP6988 @ 0x70)
  * DTU       : UART2 TX=G22 / RX=G19 @115200 (RAK3172, read-only AT)

SAFETY: this test NEVER actuates the pump.  The pump check is a passive read of
the control line (must be off).  The DTU receives read-only AT queries only.

Run with::

    bazel test //tests:test_sprout_e2e --test_output=all

(or ``--spawn_strategy=local`` if the sandbox blocks /dev/ttyUSB0)
"""

import logging
from typing import Optional

import pytest

try:
    import serial
    import serial.tools.list_ports

    HAS_PYSERIAL = True
except ImportError:  # pragma: no cover - guards against partial installs
    HAS_PYSERIAL = False

try:
    from cardputer_client import flash as cardputer_flash

    HAS_FLASH_LIB = True
except ImportError:  # pragma: no cover
    HAS_FLASH_LIB = False

_logger = logging.getLogger(__name__)

# FTDI FT232 bridge as shipped on the M5Stack Atom Lite board.
SPROUT_VID = 0x0403
SPROUT_PID = 0x6001
SPROUT_SN_SUBSTR = "69526EE94F"
SPROUT_BAUD = 115200

# Sane physical bounds for reads (functional check, not calibration).
MOISTURE_RAW_MIN = 300
MOISTURE_RAW_MAX = 4095
TEMP_C_MIN, TEMP_C_MAX = -10.0, 50.0
HUM_PCT_MAX = 105.0  # tiny tolerance for sensor edge cases
QMP_CHIP_ID = 0x5C


def find_sprout_port() -> Optional[str]:
    """Return the serial device path of the Sprout (Atom Lite) or ``None``.

    Identified by the FTDI FT232 VID/PID the M5Stack board enumerates as
    (manufacturer ``Hades2001``, product ``M5stack``), or by serial.
    """
    if not HAS_PYSERIAL:
        return None
    try:
        ports = serial.tools.list_ports.comports()
    except Exception as exc:  # pragma: no cover
        _logger.warning("Could not enumerate serial ports: %s", exc)
        return None
    for p in ports:
        try:
            if p.vid == SPROUT_VID and p.pid == SPROUT_PID:
                return p.device
        except (TypeError, AttributeError):
            pass
        blob = " ".join(
            str(getattr(p, f, "") or "")
            for f in ("manufacturer", "product", "description", "serial_number")
        ).lower()
        if "m5stack" in blob or SPROUT_SN_SUBSTR.lower() in blob:
            return p.device
    return None


_SPROUT_PORT = find_sprout_port()

if not (_SPROUT_PORT and HAS_PYSERIAL and HAS_FLASH_LIB):
    print(
        "\nSPROUT E2E: SKIP - Sprout rig not detected on this machine "
        "(no FTDI 0403:6001 'M5stack'). Attach the Sprout stack to run.\n"
    )
    pytestmark = pytest.mark.skip(
        reason="Sprout rig not detected on this machine (hardware E2E)"
    )


class _SproutFixture:
    """Thin wrapper: raw-REPL serial to the Sprout + exec_raw helper."""

    def __init__(self, ser: "serial.Serial"):
        self.ser = ser

    def on_device(self, code: str, timeout: int = 20) -> str:
        """Execute MicroPython *code* on the device; return raw output."""
        ok, output = cardputer_flash.exec_raw(self.ser, code, timeout=timeout)
        _logger.debug("exec_raw ok=%s out=%r", ok, output)
        return output


@pytest.fixture(scope="module")
def sprout() -> "_SproutFixture":
    if not _SPROUT_PORT:
        pytest.skip("Sprout rig not connected")
    ser = serial.Serial(_SPROUT_PORT, SPROUT_BAUD, timeout=1, write_timeout=10)
    assert cardputer_flash.enter_raw_repl(ser, max_attempts=5), (
        f"Could not enter raw REPL on {_SPROUT_PORT}"
    )
    yield _SproutFixture(ser)
    try:
        cardputer_flash.exit_raw_repl(ser)
    finally:
        ser.close()


def _parse_marker(output: str, marker: str) -> dict:
    """Extract ``key=value`` fields from the line containing *marker*.

    Raw-REPL output can have a leading ``OK`` glued to the marker line (the
    MicroPython raw REPL edits the input back to us), so we locate the marker
    anywhere in the output rather than requiring it at a line start.
    """
    idx = output.find(marker)
    if idx < 0:
        return {}
    line = output[idx:].splitlines()[0]
    bits = line[len(marker):].strip()
    return dict(
        seg.split("=", 1) for seg in bits.split() if "=" in seg
    )


def test_sprout_identity(sprout: "_SproutFixture"):
    """Device is an ESP32 running MicroPython."""
    out = sprout.on_device(
        "import sys,os,machine\n"
        'print("SPROUT_ID platform=%s ver=%s mac=%s" % '
        "(sys.platform, os.uname().release, machine.unique_id().hex().upper()))"
    )
    info = _parse_marker(out, "SPROUT_ID")
    assert info.get("platform") == "esp32", f"unexpected platform: {out!r}"
    assert info.get("ver"), f"missing MicroPython version: {out!r}"


def test_moisture_sensor_g32(sprout: "_SproutFixture"):
    """Moisture analog probe on G32 returns a valid ADC reading."""
    out = sprout.on_device(
        "import machine\n"
        "a=machine.ADC(machine.Pin(32)); a.atten(machine.ADC.ATTN_11DB); "
        "a.width(machine.ADC.WIDTH_12BIT)\n"
        "v=[a.read() for _ in range(8)]\n"
        'print("SPROUT_MOISTURE min=%d mean=%d max=%d" % '
        "(min(v), sum(v)//8, max(v)))"
    )
    info = _parse_marker(out, "SPROUT_MOISTURE")
    mean = int(info.get("mean", "-1"))
    assert MOISTURE_RAW_MIN <= mean <= MOISTURE_RAW_MAX, (
        f"moisture mean {mean} out of sane range; full out: {out!r}"
    )


def test_pump_line_passive_off_g26(sprout: "_SproutFixture"):
    """Pump control line (G26) is OFF -- passive read, never driven.

    This is the safety test: the Watering Unit pump runs when its control
    line is driven high/floating, so it must read LOW.  We never configure
    G26 as an output and never write to it.
    """
    out = sprout.on_device(
        "import machine\n"
        "p=machine.Pin(26, machine.Pin.IN)\n"
        'print("SPROUT_PUMP val=%d" % p.value())'
    )
    info = _parse_marker(out, "SPROUT_PUMP")
    assert info.get("val") == "0", f"pump control line is not OFF: {out!r}"


def test_env3_bus_g21_g25(sprout: "_SproutFixture"):
    """ENV III on the DTU base Port A bus (G21/G25) exposes SHT30 + QMP6988."""
    out = sprout.on_device(
        "import machine\n"
        "i2c=machine.SoftI2C(scl=machine.Pin(21), sda=machine.Pin(25), freq=100000)\n"
        "found=[]\n"
        "for ad in range(3, 0x78):\n"
        "    try:\n"
        "        i2c.readfrom(ad, 1); found.append(ad)\n"
        "    except OSError:\n"
        "        pass\n"
        'print("SPROUT_ENV3 addrs=" + ",".join("%02x" % a for a in found))'
    )
    info = _parse_marker(out, "SPROUT_ENV3")
    addrs = set(info.get("addrs", "").split(","))
    assert "44" in addrs, f"SHT30 (0x44) missing on Port A: {out!r}"
    assert "70" in addrs, f"QMP6988 (0x70) missing on Port A: {out!r}"


def test_sht30_temp_humidity(sprout: "_SproutFixture"):
    """ENV III air temperature + humidity read in sane physical ranges."""
    out = sprout.on_device(
        "import machine\n"
        "from time import sleep_ms\n"
        "i2c=machine.SoftI2C(scl=machine.Pin(21), sda=machine.Pin(25), freq=100000)\n"
        "try:\n"
        "    i2c.writeto(0x44, b'\\x2c\\x06'); sleep_ms(60)\n"
        "    d=i2c.readfrom(0x44, 6)\n"
        "    t=-45 + 175*(((d[0]<<8)|d[1])/65535)\n"
        "    h=100*(((d[3]<<8)|d[4])/65535)\n"
        '    print("SPROUT_SHT30 temp_c=%.2f hum_pct=%.2f" % (t, h))\n'
        "except Exception as e:\n"
        '    print("SPROUT_SHT30 ERR %s" % type(e).__name__)'
    )
    info = _parse_marker(out, "SPROUT_SHT30")
    if "ERR" in info:
        pytest.fail(f"SHT30 read failed: {out!r}")
    temp = float(info.get("temp_c", "nan"))
    hum = float(info.get("hum_pct", "nan"))
    assert TEMP_C_MIN <= temp <= TEMP_C_MAX, f"temperature out of range: {out!r}"
    assert 0.0 <= hum <= HUM_PCT_MAX, f"humidity out of range: {out!r}"


def test_qmp6988_chip_id(sprout: "_SproutFixture"):
    """QMP6988 chip-ID is clean 0x5C -- proves the 0x70 mux collision is gone."""
    out = sprout.on_device(
        "import machine\n"
        "i2c=machine.SoftI2C(scl=machine.Pin(21), sda=machine.Pin(25), freq=100000)\n"
        "try:\n"
        "    v=i2c.readfrom_mem(0x70, 0xD1, 1)[0]\n"
        '    print("SPROUT_QMP chip_id=0x%02x" % v)\n'
        "except Exception as e:\n"
        '    print("SPROUT_QMP ERR %s" % type(e).__name__)'
    )
    info = _parse_marker(out, "SPROUT_QMP")
    if "ERR" in info:
        pytest.fail(f"QMP6988 read failed: {out!r}")
    assert int(info.get("chip_id", "0x0"), 16) == QMP_CHIP_ID, (
        f"QMP chip-ID not 0x5C (bus corrupted?): {out!r}"
    )


def test_dtu_lora_at_alive(sprout: "_SproutFixture"):
    """DTU (RAK3172) answers the read-only AT probe over the 9-pin stack."""
    out = sprout.on_device(
        "import machine\n"
        "from time import sleep_ms\n"
        "u=machine.UART(2, 115200, tx=machine.Pin(22), rx=machine.Pin(19), timeout=100)\n"
        "def rep(c):\n"
        "    u.write(c)\n"
        "    t=0\n"
        "    while u.any()==0 and t<20:\n"
        "        sleep_ms(100); t+=1\n"
        "    sleep_ms(150)\n"
        "    return u.read(u.any()).decode().strip().replace(chr(10),'|')\n"
        "r=rep(b'AT\\r\\n')\n"
        "r2=rep(b'AT+VER=?\\r\\n')\n"
        "print('SPROUT_DTU at=%s ver=%s' % (r, r2))"
    )
    info = _parse_marker(out, "SPROUT_DTU")
    at = info.get("at", "")
    assert "OK" in at, f"DTU did not answer AT: {out!r}"
    _logger.info("Sprout E2E DTU: AT=%r VER=%r", info.get("at"), info.get("ver"))


if __name__ == "__main__":  # Bazel py_test runs this file directly
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
