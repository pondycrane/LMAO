"""E2E test for the native C Cardputer firmware ↔ Pi Server LoRa communication.

Replaces the MicroPython-era loRa E2E for the PR1 sensor-node firmware
(cardputer_client/firmware/): build+flash the native image with the test
server's DEST_HASH baked in, then verify the server receives a SensorReport
(real ESP32 die temperature) over the LoRa RNode and ingests it into DuckDB.

Requires hardware: a Cardputer (USB-Serial-JTAG /dev/ttyACM0) + a Heltec RNode
(LoRa radio).  Auto-skips when neither is detected.  Unlike the legacy
MicroPython E2E there is no raw REPL / config.py patching — the native firmware
is flashed in download mode via the ESP-IDF toolchain (Docker), and its
settings are build-time defines.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

_logger = logging.getLogger(__name__)

try:
    import serial

    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

get_config_dict = None
try:
    from lmao_server.config import get_config_dict as _get_config_dict

    get_config_dict = _get_config_dict
except ImportError:
    get_config_dict = None

contextlib = __import__("contextlib")

_sys_path = os.path.dirname(__file__)
if _sys_path not in sys.path:
    sys.path.insert(0, _sys_path)
from e2e_helpers import (  # noqa: E402
    find_rnode_port,
)

# ── hardware detection ──────────────────────────────────────────────


def _find_cardputer_port():
    """Return the device path of a connected Cardputer, or *None*."""
    try:
        if serial.tools.list_ports.comports:
            for p in serial.tools.list_ports.comports():
                # Cardputer ADV = M5Stack Stamp-S3A (VID 0x303A espressif),
                # USB-Serial-JTAG console.  Match by description keyword, or the
                # M5Stack bootloader descriptor (0x8120) / the generic native
                # ESP32-S3 USB-Serial-JTAG descriptor (0x1001) that idf.py flash
                # leaves — both are this device depending on its firmware.
                desc = (p.description or "").lower()
                if "cardputer" in desc:
                    return p.device
                if getattr(p, "vid", None) == 0x303A and getattr(p, "pid", None) in (0x8120, 0x1001):
                    return p.device
    except Exception as exc:
        _logger.warning("Cardputer port scan failed: %s", exc)
    return None


_RNODE_PORT = find_rnode_port() if HAS_PYSERIAL else None
_CARDCOMPUTER_PORT = _find_cardputer_port() if HAS_PYSERIAL else None
_HARDWARE_CHECKED = False
_HARDWARE_READY = False
_HARDWARE_REASON = None


def _probe_hardware():
    """Probe for both Cardputer (native firmware connected) and Heltec RNode."""
    global _HARDWARE_CHECKED, _HARDWARE_READY, _HARDWARE_REASON
    if _HARDWARE_CHECKED:
        return
    _HARDWARE_CHECKED = True

    if not HAS_PYSERIAL:
        _HARDWARE_REASON = "pyserial not available"
        return
    if _CARDCOMPUTER_PORT is None or not os.path.exists(_CARDCOMPUTER_PORT):
        _HARDWARE_REASON = f"No Cardputer detected (looked at {_CARDCOMPUTER_PORT})"
        return
    if _RNODE_PORT is None or not os.path.exists(_RNODE_PORT):
        _HARDWARE_REASON = f"No Heltec RNode detected (looked at {_RNODE_PORT})"
        return
    if os.environ.get("LMAO_SKIP_NATIVE_CARDPUTER_E2E"):
        _HARDWARE_REASON = "E2E disabled by LMAO_SKIP_NATIVE_CARDPUTER_E2E"
        return
    _HARDWARE_READY = True


def _hardware_required():
    _probe_hardware()
    return _HARDWARE_REASON


# ── native build/flash helpers ──────────────────────────────────────


def _firmware_dir():
    """Absolute path of cardputer_client/firmware (workspace-aware)."""
    for cand in (
        os.environ.get("BUILD_WORKSPACE_DIRECTORY"),
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ):
        if not cand:
            continue
        d = os.path.join(cand, "cardputer_client", "firmware")
        if os.path.isfile(os.path.join(d, "build.sh")):
            return d
    raise AssertionError("Cannot locate cardputer_client/firmware/ directory")


def _run_script(script, env, port=None):
    """Run a firmware script (build.sh / flash.sh) with the given env."""
    fw_dir = _firmware_dir()
    cmd = ["bash", os.path.join(fw_dir, script)]
    if port:
        cmd += ["--port", port]
    if os.environ.get("LMAO_VERBOSE"):
        proc = subprocess.run(cmd, env=env, cwd=fw_dir, text=True)
    else:
        proc = subprocess.run(
            cmd, env=env, cwd=fw_dir, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-3000:]
        raise RuntimeError(f"{script} failed (exit {proc.returncode}):\n{tail}")
    return proc


def build_native(dest_hash=None, interval=300, sensor_type=0):
    """Build the native firmware, optionally baking DEST_HASH/interval/sensor."""
    env = dict(os.environ)
    if dest_hash:
        env["LMAO_DEST_HASH_HEX"] = dest_hash
    env["LMAO_INTERVAL_SECONDS"] = str(interval)
    env["LMAO_SENSOR_TYPE"] = str(sensor_type)
    _run_script("build.sh", env)


def flash_native(port):
    """Flash the already-built native firmware to *port*."""
    env = dict(os.environ)
    _run_script("flash.sh", env, port=port)


# ── tests ───────────────────────────────────────────────────────────


class TestNativeCardputerE2E:
    """Tests that require Cardputer (native firmware) + Heltec RNode."""

    @pytest.fixture(autouse=True)
    def skip_if_no_hardware(self):
        reason = _hardware_required()
        if reason:
            pytest.skip(reason)

    def test_native_firmware_sources_exist(self):
        """The native firmware source tree must exist for flash."""
        fw = _firmware_dir()
        for rel in ("build.sh", "flash.sh", "main/main.cpp", "main/lora_interface.cpp"):
            assert os.path.isfile(os.path.join(fw, rel)), f"missing {rel}"

    def test_native_lora_full_e2e(self):
        """Full LoRa E2E: build+flash native firmware with server hash, start
        an in-process RNS+LXMF server, and verify a SensorReport (real die
        temp) arrives over the RNode and lands in DuckDB.

        This is a SINGLE comprehensive test (RNS is a process-wide singleton).
        1. Start a temporary server with the Heltec RNode.
        2. Build + flash the native firmware with the test server's DEST_HASH
           and a 15s interval.
        3. Monitor serial for the native boot banner.
        4. Assert the server received a SensorReport; validate die temp in the
           realistic ESP32 range and DuckDB ingestion.
        5. Restore the production build (production DEST_HASH, 300s interval).
        """
        import LXMF
        import RNS

        from lma_core import LMAOEnvelope
        from lma_core.config_utils import dict_to_ini
        from lma_core.storage import DuckDbStore

        import google.protobuf.message

        # ── Setup: prepare server config ──
        cfg_dict = get_config_dict()
        rnode_port = cfg_dict["interfaces"]["RNode LoRa"]["port"]
        if not os.path.exists(rnode_port) and _RNODE_PORT:
            cfg_dict["interfaces"]["RNode LoRa"]["port"] = _RNODE_PORT
            rnode_port = _RNODE_PORT
        if not os.path.exists(rnode_port):
            pytest.skip(f"RNode port {rnode_port} not available")

        configdir = tempfile.mkdtemp(prefix="lmao_native_e2e_rns_")
        db_path = None
        try:
            config_content = dict_to_ini(
                {
                    "logging": {"loglevel": 7},
                    "transport": {"path": "/tmp/lmao_native_e2e_rns_state"},
                },
                {"RNode LoRa": cfg_dict["interfaces"]["RNode LoRa"]},
            )
            with open(os.path.join(configdir, "config"), "w") as f:
                f.write(config_content)

            RNS.Reticulum(configdir=configdir)
            identity = RNS.Identity()
            router = LXMF.LXMRouter(identity=identity, storagepath="/tmp/lmao_native_e2e_lxmf")
            router.register_delivery_identity(identity, display_name="lmao-native-e2e-server")
            server_dest_hash_bytes = next(iter(router.delivery_destinations))
            server_hash = RNS.hexrep(server_dest_hash_bytes, delimit=False)

            received_messages = []
            sensor_messages = []
            store_failures = 0
            message_event = __import__("threading").Event()

            db_fd, db_path = tempfile.mkstemp(suffix=".duckdb", prefix="lmao_native_e2e_")
            os.close(db_fd)
            os.unlink(db_path)  # DuckDB refuses an existing empty file
            store = DuckDbStore()
            store.initialize(db_path)

            def capture_delivery(message):
                nonlocal store_failures
                source = message.get_source()
                source_hash = (
                    RNS.hexrep(source.hash, delimit=False) if source else "<unknown>"
                )
                content_bytes = message.content if hasattr(message, "content") else b""
                try:
                    envelope = LMAOEnvelope()
                    envelope.ParseFromString(content_bytes)
                except (google.protobuf.message.DecodeError, Exception):
                    display_text = content_bytes.decode("utf-8", errors="replace")
                else:
                    if envelope.HasField("sensor"):
                        display_text = (
                            f"SensorReport(seq={envelope.sensor.seq}, "
                            f"readings={len(envelope.sensor.readings)})"
                        )
                        try:
                            asyncio.run(store.store_sensor_report(bytes(content_bytes)))
                            sensor_messages.append(
                                {
                                    "source": source_hash,
                                    "node_id": envelope.sensor.node_id,
                                    "seq": envelope.sensor.seq,
                                }
                            )
                        except Exception:
                            store_failures += 1
                    elif envelope.HasField("text"):
                        display_text = envelope.text.content
                    else:
                        display_text = content_bytes.decode("utf-8", errors="replace")
                received_messages.append(
                    {"source": source_hash, "content": display_text, "raw": content_bytes}
                )
                message_event.set()

            router.register_delivery_callback(capture_delivery)
            router.announce(server_dest_hash_bytes)

            # ── Build + flash the native firmware with the test server hash ──
            print(f"\n[Native E2E] server DEST_HASH: {server_hash}")
            printer = print
            printer("Building native firmware with test DEST_HASH + 15s interval ...")
            build_native(dest_hash=server_hash, interval=15)
            printer(f"Flashing native firmware to {_CARDCOMPUTER_PORT} ...")
            flash_native(_CARDCOMPUTER_PORT)

            # ── Monitor serial for the native boot banner ──
            cardputer_output = b""
            found_banner = False
            serial_deadline = time.time() + 75
            last_announce = 0.0

            ser = serial.Serial(_CARDCOMPUTER_PORT, 115200, timeout=1, write_timeout=10)
            try:
                while time.time() < serial_deadline:
                    if time.time() - last_announce > 8:
                        try:
                            router.announce(server_dest_hash_bytes)
                        except Exception:
                            pass
                        last_announce = time.time()
                    try:
                        if ser.in_waiting:
                            cardputer_output += ser.read(ser.in_waiting)
                    except OSError:
                        # Device may hard-reset / re-enumerate after flash;
                        # reopen and keep monitoring.
                        _logger.info("Cardputer serial lost — waiting for re-enumeration...")
                        with contextlib.suppress(Exception):
                            ser.close()
                        ser = None
                        while time.time() < serial_deadline:
                            if os.path.exists(_CARDCOMPUTER_PORT):
                                try:
                                    ser = serial.Serial(
                                        _CARDCOMPUTER_PORT, 115200, timeout=1, write_timeout=10
                                    )
                                    break
                                except Exception:
                                    pass
                            time.sleep(1.0)
                        if ser is None:
                            raise
                        _logger.info("Cardputer serial reopened.")
                        continue

                    if b"Cardputer native client starting" in cardputer_output:
                        found_banner = True

                    if message_event.is_set():
                        remaining = serial_deadline - time.time()
                        if found_banner and remaining < 5:
                            break
                        time.sleep(0.25)
                        continue
                    time.sleep(0.25)
            finally:
                if ser is not None:
                    with contextlib.suppress(Exception):
                        ser.close()

            captured = cardputer_output.decode("utf-8", errors="replace")
            print(f"\n[Cardputer serial output — {len(cardputer_output)} bytes]")
            print(captured[:6000])

            # ── Assertions ──
            assert found_banner, (
                "Cardputer did not show the native boot banner after flashing.\n"
                f"Captured: {captured[:500]}"
            )
            assert len(sensor_messages) > 0, (
                "Server did not receive any SensorReport from the native Cardputer.\n"
                f"Received {len(received_messages)} messages.\n"
                f"Cardputer serial: {captured[:1000]}"
            )

            # ── DuckDB sensor data: validate real die temperature ──
            rows = asyncio.run(
                store.query(
                    "SELECT node_id, value, unit FROM sensor_readings "
                    "ORDER BY timestamp_ms DESC LIMIT 5"
                )
            )
            assert len(rows) > 0, (
                "No sensor data found in DuckDB after native E2E. "
                f"Received {len(sensor_messages)} SensorReport envelope(s)."
            )
            for row in rows:
                temp = float(row[1])
                assert 15 <= temp <= 85, (
                    f"Expected real ESP32 die temperature in [15, 85]°C, got {temp}°C."
                )
                assert temp != 25.0, (
                    f"Temperature {temp}°C equals the CPython fallback constant — "
                    "native TSENS must be used."
                )
            # Humidity rows present only when a DHT20 is attached/sensored.
            humidity_rows = [r for r in rows if len(r) >= 3 and r[2] == "%"]
            for row in humidity_rows:
                assert 0.0 <= float(row[1]) <= 100.0

            assert store_failures == 0, (
                f"DuckDB store failed {store_failures} time(s)."
            )
            print(f"\n✅ Native LoRa E2E passed: {len(sensor_messages)} SensorReport(s), "
                  f"{len(rows)} row(s) in DuckDB")

            # ── Restore production build (production DEST_HASH + default interval) ──
            try:
                from lma_core.server_identity import ensure_delivery_destination_hash

                prod_hash = ensure_delivery_destination_hash()
                print("Restoring production firmware build (DEST_HASH, 300s interval) ...")
                build_native(dest_hash=prod_hash, interval=300)
                flash_native(_CARDCOMPUTER_PORT)
            except Exception as exc:
                _logger.warning("Production restore failed: %s", exc, exc_info=True)

        finally:
            try:
                store.close()
            except Exception:
                pass
            with contextlib.suppress(OSError):
                if db_path:
                    os.unlink(db_path)
            shutil.rmtree(configdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
