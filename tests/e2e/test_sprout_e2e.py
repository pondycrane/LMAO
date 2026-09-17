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

Two kinds of gate here:

* **USB-probe tests** (identity / moisture / pump / DTU-AT): require a Sprout
  that answers the MicroPython raw REPL.  The Sprout now runs the native-client
  firmware (C++, issue #130+) which does NOT expose a raw REPL, so these
  loud-skip on such a rig.
* **Pushed-reading test** (``TestSproutPushedAirTemp``): the architecture-true
  check for the ENV III air temp/humidity.  The native firmware reads the SHT30
  and pushes an LXMF SensorReport over LoRa to the in-cluster server RNode
  (~60s cadence); the server persists it to DuckDB via NATS.  This test listens
  on the in-cluster query API for a fresh pushed reading with sane physical
  values — it never re-initialises the ENV III I2C bus over USB (which would
  disturb the firmware that now owns it).  Requires ``kubectl`` + a reachable
  cluster; loud-skips otherwise.

SAFETY: this test NEVER actuates the pump.  The pump check is a passive read of
the control line (must be off).  The DTU receives read-only AT queries only.

Run with::

    bazel test //tests:test_sprout_e2e --test_output=all --spawn_strategy=local \
        --test_env=KUBECONFIG=$HOME/.kube/config

(``--spawn_strategy=local`` lets the sandbox see the USB serial; the
KUBECONFIG env is needed so ``kubectl`` (used by the pushed-reading test)
can reach the in-cluster query API.)
"""

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.request
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
# (VID 0x0403 / PID 0x6001 — but detection uses the board's USB descriptor
# strings, not raw VID/PID, so a generic FTDI does not false-positive.)
SPROUT_SN_SUBSTR = "69526EE94F"
SPROUT_BAUD = 115200

# Sane physical bounds for reads (functional check, not calibration).
MOISTURE_RAW_MIN = 300
MOISTURE_RAW_MAX = 4095
TEMP_C_MIN, TEMP_C_MAX = -10.0, 50.0
HUM_PCT_MAX = 105.0  # tiny tolerance for sensor edge cases


def find_sprout_port() -> Optional[str]:
    """Return the serial device path of the Sprout (Atom Lite) or ``None``.

    The Sprout is an M5Stack Atom Lite that enumerates as an FTDI FT232
    (VID 0x0403, PID 0x6001) with manufacturer ``Hades2001``, product
    ``M5stack`` and serial ``69526EE94F``.  VID/PID alone is NOT sufficient:
    any FTDI bridge (e.g. a generic FT232 that is not the Sprout) matches,
    which turns a non-Sprout host into a hard failure instead of a clean
    skip.  We therefore require descriptor evidence of the M5Stack board.
    """
    if not HAS_PYSERIAL:
        return None
    try:
        ports = serial.tools.list_ports.comports()
    except Exception as exc:  # pragma: no cover
        _logger.warning("Could not enumerate serial ports: %s", exc)
        return None
    for p in ports:
        blob = " ".join(
            str(getattr(p, f, "") or "")
            for f in ("manufacturer", "product", "description", "serial_number")
        ).lower()
        if SPROUT_SN_SUBSTR.lower() in blob or "m5stack" in blob or "hades2001" in blob:
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
    if not cardputer_flash.enter_raw_repl(ser, max_attempts=5):
        ser.close()
        pytest.skip(
            "Sprout does not answer the MicroPython raw REPL — it is running the "
            "native-client firmware (C++, issue #130+). USB-probe tests only apply "
            "to a MicroPython Sprout; the pushed-reading test "
            "(TestSproutPushedAirTemp) verifies the ENV III measurement instead."
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


class TestSproutPushedAirTemp:
    """ENV III air temp + humidity verified from the PUSHED path.

    The native-client Sprout firmware (issue #130+) owns the ENV III I2C bus,
    reads SHT30 air temp/humidity, and PUSHES an LXMF SensorReport over LoRa
    to the in-cluster server RNode every ~60s.  The server encodes air temp as
    ``sensor_id 3`` (unit C) and humidity as ``sensor_id 2`` (unit %) in the
    protobuf SensorReport, and the iot-ingest consumer persists each reading
    to the ``sensor_readings`` DuckDB table via NATS.

    Probing the ENV III bus over the USB raw REPL is obsolete (the new
    firmware owns the bus, so re-initialising SoftI2C can disturb the live
    reading) — this test LISTENS for a fresh pushed reading to land in DuckDB
    instead, asserting the full Sprout → LoRa → RNode → server → NATS →
    DuckDB path with sane physical values.
    """

    PUSH_WINDOW_S = 200      # Enough for >= 2 pushes at the ~60-90 s cadence.
    POLL_INTERVAL_S = 15

    # -- query-API plumbing ----------------------------------------

    def _start_port_forward(self):
        """Port-forward the in-cluster iot-query API on an ephemeral port.

        kubectl picks the local port (``0:8080``) so parallel runs never
        collide; the chosen port is parsed from the ``Forwarding from`` line.
        """
        proc = subprocess.Popen(
            ["kubectl", "port-forward", "svc/iot-query", "0:8080"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        port = None
        for _ in range(60):
            line = proc.stdout.readline().strip()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
                continue
            print(f"  kubectl port-forward: {line}")
            match = re.search(r"127\.0\.0\.1:(\d+)", line)
            if match:
                port = int(match.group(1))
                break
        if port is None:
            proc.terminate()
            return None, None
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2
                ) as resp:
                    if resp.status == 200:
                        return proc, port
            except Exception:
                pass
            time.sleep(0.5)
        proc.terminate()
        return None, None

    def _query(self, port: int, sql: str) -> list[dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/query",
            data=json.dumps({"sql": sql}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        if "error" in body:
            raise RuntimeError(f"query API error: {body['error']}")
        cols = body["columns"]
        return [dict(zip(cols, row)) for row in body["rows"]]

    def _max_air_temp_ingest(self, port: int) -> Optional[str]:
        rows = self._query(
            port,
            "SELECT MAX(ingested_at) AS m FROM sensor_readings WHERE sensor_id = 3",
        )
        return (rows[0].get("m") if rows else None) or None

    # -- fixture ---------------------------------------------------

    @pytest.fixture(autouse=True)
    def _gate(self):
        """Require the Sprout rig AND a reachable cluster with the pipeline."""
        if not _SPROUT_PORT:
            pytest.skip("Sprout rig not connected — no pushed readings to observe")
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not found — cannot reach the in-cluster pipeline")
        result = subprocess.run(
            ["kubectl", "cluster-info"], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            pytest.skip("K8s cluster unreachable — cannot verify pushed readings")

    # -- test ------------------------------------------------------

    def test_air_temp_and_humidity_arrive_in_duckdb(self):
        pf, port = self._start_port_forward()
        if pf is None:
            pytest.skip("Could not port-forward svc/iot-query (pipeline query API down)")
        try:
            baseline = self._max_air_temp_ingest(port)
            print(
                f"Sprout pushed-reading baseline (latest air-temp ingest): {baseline!r}"
            )
            deadline = time.time() + self.PUSH_WINDOW_S
            seen: dict = {}
            while time.time() < deadline:
                try:
                    rows = self._query(
                        port,
                        "SELECT node_id, sensor_id, value, unit, ingested_at "
                        "FROM sensor_readings "
                        "WHERE sensor_id IN (2, 3) "
                        "ORDER BY ingested_at DESC",
                    )
                except Exception as exc:  # transient poll error — keep waiting
                    print(f"  poll query error: {exc}")
                    time.sleep(self.POLL_INTERVAL_S)
                    continue
                fresh = [
                    r for r in rows
                    if baseline is None or (r.get("ingested_at") or "") > baseline
                ]
                for r in fresh:
                    seen[(r["node_id"], r["sensor_id"])] = r
                air = next((r for (n, s), r in seen.items() if s == 3), None)
                hum = next((r for (n, s), r in seen.items() if s == 2), None)
                if (
                    air is not None
                    and hum is not None
                    and air["node_id"] == hum["node_id"]
                ):
                    temp = float(air["value"])
                    humidity = float(hum["value"])
                    assert TEMP_C_MIN <= temp <= TEMP_C_MAX, (
                        f"pushed air temp {temp} C out of range: {air}"
                    )
                    assert 0.0 <= humidity <= HUM_PCT_MAX, (
                        f"pushed humidity {humidity} % out of range: {hum}"
                    )
                    print(
                        f"PUSHED OK node={air['node_id'][:8]} "
                        f"temp={temp:.2f} C hum={humidity:.2f} % "
                        f"at {air['ingested_at']}"
                    )
                    return
                time.sleep(self.POLL_INTERVAL_S)
            pytest.fail(
                "No fresh Sprout air-temp/humidity reading landed in DuckDB within "
                f"{self.PUSH_WINDOW_S}s (baseline {baseline!r}). Is the Sprout rig "
                "powered and pushing SensorReports over LoRa (native-client "
                f"firmware)? Fresh rows seen: {seen!r}"
            )
        finally:
            pf.terminate()
            try:
                pf.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pf.kill()


if __name__ == "__main__":  # Bazel py_test runs this file directly
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
