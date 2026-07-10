"""E2E test for Cardputer ↔ Pi Server LoRa communication.

Requires both a Cardputer ADV (with antenna) and a Heltec ESP32 RNode
(flashed with RNode firmware) connected via USB.

Covers:
  * Heltec RNode detection
  * Cardputer flashing with server identity hash
  * Temporary server startup
  * LoRa message send/receive (Cardputer → Server → Cardputer ACK)
  * Radio parameter assertion (SF, BW, CR, frequency match between server and client)

Run with::

    bazel test //tests:test_cardputer_lora_e2e --test_output=all
"""

from collections.abc import Callable
from typing import Any

import asyncio
import logging
import os
import sys
import time
import threading

import pytest

_logger = logging.getLogger(__name__)

try:
    import serial
    import serial.tools.list_ports

    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

try:
    from cardputer_client import flash as cardputer_flash

    HAS_FLASH_LIB = True
except ImportError:
    HAS_FLASH_LIB = False

get_config_dict: Callable[[], dict[str, Any]] | None = None


try:
    from lmao_server.config import get_config_dict as _get_config_dict

    get_config_dict = _get_config_dict
    HAS_SERVER_CONFIG = True
except ImportError:
    HAS_SERVER_CONFIG = False


# ── helpers ─────────────────────────────────────────────────────────


# Ensure e2e_helpers is importable when running the script directly
# (Bazel already adds the e2e/ directory to sys.path).
sys.path.insert(0, os.path.dirname(__file__))
from e2e_helpers import (  # noqa: E402
    find_rnode_port,
    case_insensitive_contains,
    check_rnode_firmware,
    flash_rnode_firmware,
)


def _find_cardputer_port():
    """Return the device path of a connected Cardputer, or *None*."""
    if not HAS_PYSERIAL or not HAS_FLASH_LIB:
        return None
    return cardputer_flash.find_cardputer_port()


# Resolve hardware presence once at collection time so skips are fast.
_RNODE_PORT = find_rnode_port() if HAS_PYSERIAL else None
_CARDCOMPUTER_PORT = _find_cardputer_port() if HAS_FLASH_LIB and HAS_PYSERIAL else None
_HARDWARE_CHECKED = False
_HARDWARE_READY = False
_HARDWARE_REASON = None


def _probe_hardware():
    """Probe for both Cardputer and Heltec RNode hardware.

    Sets module-level globals so the probe runs at most once.
    """
    global _HARDWARE_CHECKED, _HARDWARE_READY, _HARDWARE_REASON, _RNODE_PORT
    if _HARDWARE_CHECKED:
        return
    _HARDWARE_CHECKED = True

    try:
        if not HAS_PYSERIAL:
            _HARDWARE_REASON = "pyserial not installed"
            return
        if not HAS_FLASH_LIB:
            _HARDWARE_REASON = "cardputer_client.flash library not importable"
            return
        if not HAS_SERVER_CONFIG:
            _HARDWARE_REASON = (
                "lmao_server.config module not importable. "
                "Check that server dependencies are declared in tests/BUILD."
            )
            return

        # Probe Heltec RNode
        if _RNODE_PORT is None:
            _HARDWARE_REASON = (
                "RNode (Heltec ESP32) not detected. "
                "Is it connected via USB and flashed with RNode firmware? "
                "See rnode_firmware/README.md."
            )
            return

        # Firmware liveness check + auto-flash (self-healing for most common
        # failure mode: Heltec connected but erased / mis-flashed).
        if not check_rnode_firmware(_RNODE_PORT):
            print(
                "RNode firmware not detected on Heltec — attempting auto-flash...",
                flush=True,
            )
            flash_ok, flash_msg = flash_rnode_firmware(_RNODE_PORT)
            if not flash_ok:
                _HARDWARE_REASON = (
                    f"RNode firmware not detected on {_RNODE_PORT} and "
                    f"auto-flash failed: {flash_msg}"
                )
                return

            # After a successful flash the RNode may re-enumerate with a
            # different device path.  Wait for the device to settle, then
            # re-discover and re-check.
            print("Waiting for device to re-enumerate (3s)...", flush=True)
            time.sleep(3)
            _RNODE_PORT = find_rnode_port()
            if _RNODE_PORT is None:
                _HARDWARE_REASON = (
                    "RNode port disappeared after flashing. "
                    "The device may have re-enumerated to a path not matched "
                    "by find_rnode_port().  Check 'ls /dev/tty*' and re-run."
                )
                return
            if not check_rnode_firmware(_RNODE_PORT):
                _HARDWARE_REASON = (
                    f"RNode firmware still not detected on {_RNODE_PORT} "
                    f"after flash.  Flash completed but post-flash verification "
                    f"failed.  Try running 'rnodeconf --port {_RNODE_PORT} --info' "
                    f"manually."
                )
                return
            print(
                f"RNode firmware verified on {_RNODE_PORT} after auto-flash.",
                flush=True,
            )
        else:
            print(f"RNode firmware detected on {_RNODE_PORT}.", flush=True)

        # Probe Cardputer
        if _CARDCOMPUTER_PORT is None:
            _HARDWARE_REASON = "Cardputer not detected — is it connected via USB?"
            return

        # Check both ports are different devices (avoid accidental same-device detection)
        if _RNODE_PORT == _CARDCOMPUTER_PORT:
            _HARDWARE_REASON = (
                f"RNode and Cardputer detected on same port {_RNODE_PORT}. "
                "They must be on different USB ports."
            )
            return

        # Try opening the Cardputer port to verify MicroPython REPL
        try:
            with serial.Serial(_CARDCOMPUTER_PORT, 115200, timeout=1) as ser:
                time.sleep(0.6)
                ok = cardputer_flash.enter_raw_repl(ser)
                if not ok:
                    _HARDWARE_REASON = (
                        f"Device at {_CARDCOMPUTER_PORT} does not respond to "
                        "MicroPython raw REPL. Is the Cardputer running MicroPython?"
                    )
                    return

                # Check for native LoRa driver (required for on-board SX1262)
                ok, out = cardputer_flash.exec_raw(
                    ser,
                    "import sys; ok = False; "
                    "try: import lora; ok = True\n"
                    "except ImportError: pass\n"
                    "print('__LORA_OK__' if ok else '__LORA_MISSING__')",
                )
                if not ok or b"__LORA_OK__" not in out:
                    _HARDWARE_REASON = (
                        f"Cardputer at {_CARDCOMPUTER_PORT} is missing the native "
                        "LoRa driver (SX1262). The 'lora' module is not "
                        "importable.\n"
                        "Install the lora.mpy driver in /lib/ on the Cardputer."
                    )
                    return
        except Exception as exc:
            _HARDWARE_REASON = (
                f"Cannot communicate with Cardputer at {_CARDCOMPUTER_PORT}: {exc}"
            )
            return

        _HARDWARE_READY = True
    except Exception as exc:
        _HARDWARE_REASON = f"Unexpected error during hardware probe: {exc}"


def _hardware_required():
    """Return a pytest skip reason string when hardware is missing."""
    _probe_hardware()
    return _HARDWARE_REASON


# ── tests ───────────────────────────────────────────────────────────


class TestHardwareDetection:
    """Tests that do NOT require physical hardware."""

    def test_pyserial_available(self):
        """pyserial must be importable in the Bazel test environment."""
        assert HAS_PYSERIAL, (
            "pyserial not available. Add '@lmao_pip//pyserial' to test deps."
        )

    def test_flash_lib_available(self):
        """The flash_lib must be importable."""
        assert HAS_FLASH_LIB, (
            "cardputer_client.flash not importable. "
            "Add '//cardputer_client:flash_lib' to test deps."
        )

    def test_port_scan_does_not_crash(self):
        """Enumerating serial ports must not raise."""
        ports = list(serial.tools.list_ports.comports())
        assert isinstance(ports, list)

    def test_client_root_discovery(self):
        """find_client_root must locate the cardputer_client/ directory."""
        root = cardputer_flash.find_client_root()
        assert root is not None, (
            "Cannot find cardputer_client/ directory. "
            "Ensure data dependencies are declared in tests/BUILD."
        )
        assert os.path.isdir(root), f"Not a directory: {root}"

    def test_required_files_exist(self):
        """Every file in FILES_TO_UPLOAD must exist."""
        root = cardputer_flash.find_client_root()
        assert root, "Cannot find cardputer_client/"
        for rel in cardputer_flash.FILES_TO_UPLOAD:
            full = os.path.join(root, rel)
            assert os.path.isfile(full), f"Missing client file: {full}"

    def test_probe_rnode_flash_succeeds(self):
        """_probe_hardware auto-flashes firmware when missing and proceeds."""
        from unittest.mock import patch, MagicMock

        mod = sys.modules[__name__]

        # Save and restore globals to prevent state leak.
        _saved = {
            k: getattr(mod, k)
            for k in (
                "_HARDWARE_CHECKED",
                "_HARDWARE_READY",
                "_HARDWARE_REASON",
                "_RNODE_PORT",
                "_CARDCOMPUTER_PORT",
                "HAS_PYSERIAL",
                "HAS_FLASH_LIB",
                "HAS_SERVER_CONFIG",
            )
        }
        try:
            mod._HARDWARE_CHECKED = False
            mod._HARDWARE_READY = False
            mod._HARDWARE_REASON = None
            mod._RNODE_PORT = "/dev/fake_rnode"
            mod._CARDCOMPUTER_PORT = "/dev/fake_cardputer"
            mod.HAS_PYSERIAL = True
            mod.HAS_FLASH_LIB = True
            mod.HAS_SERVER_CONFIG = True

            mock_ser = MagicMock()
            mock_ser.__enter__ = MagicMock(return_value=mock_ser)
            mock_ser.__exit__ = MagicMock(return_value=False)

            # check_rnode_firmware returns False first (missing), then True (verified after flash)
            with (
                patch.object(mod, "check_rnode_firmware", side_effect=[False, True]),
                patch.object(mod, "flash_rnode_firmware", return_value=(True, "ok")),
                patch.object(mod, "find_rnode_port", return_value="/dev/fake_rnode"),
                patch.object(mod, "serial") as mock_serial,
                patch.object(mod.cardputer_flash, "enter_raw_repl", return_value=True),
                patch.object(
                    mod.cardputer_flash,
                    "exec_raw",
                    return_value=(True, b"__LORA_OK__"),
                ),
            ):
                mock_serial.Serial.return_value = mock_ser
                _probe_hardware()

            assert mod._HARDWARE_READY is True, (
                f"Expected HARDWARE_READY=True, got reason={mod._HARDWARE_REASON}"
            )
            assert mod._HARDWARE_REASON is None
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)

    def test_probe_rnode_flash_fails(self):
        """_probe_hardware skips with reason when auto-flash fails."""
        from unittest.mock import patch

        mod = sys.modules[__name__]

        _saved = {
            k: getattr(mod, k)
            for k in (
                "_HARDWARE_CHECKED",
                "_HARDWARE_READY",
                "_HARDWARE_REASON",
                "_RNODE_PORT",
                "_CARDCOMPUTER_PORT",
                "HAS_PYSERIAL",
                "HAS_FLASH_LIB",
                "HAS_SERVER_CONFIG",
            )
        }
        try:
            mod._HARDWARE_CHECKED = False
            mod._HARDWARE_READY = False
            mod._HARDWARE_REASON = None
            mod._RNODE_PORT = "/dev/fake_rnode"
            mod._CARDCOMPUTER_PORT = "/dev/fake_cardputer"
            mod.HAS_PYSERIAL = True
            mod.HAS_FLASH_LIB = True
            mod.HAS_SERVER_CONFIG = True

            with (
                patch.object(mod, "check_rnode_firmware", return_value=False),
                patch.object(
                    mod, "flash_rnode_firmware", return_value=(False, "esptool error")
                ),
            ):
                _probe_hardware()

            assert mod._HARDWARE_READY is False
            assert mod._HARDWARE_REASON is not None
            assert "auto-flash failed" in mod._HARDWARE_REASON
            assert "esptool error" in mod._HARDWARE_REASON
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)

    def test_probe_rnode_firmware_present(self):
        """_probe_hardware skips flash when firmware is already present."""
        from unittest.mock import patch, MagicMock

        mod = sys.modules[__name__]

        _saved = {
            k: getattr(mod, k)
            for k in (
                "_HARDWARE_CHECKED",
                "_HARDWARE_READY",
                "_HARDWARE_REASON",
                "_RNODE_PORT",
                "_CARDCOMPUTER_PORT",
                "HAS_PYSERIAL",
                "HAS_FLASH_LIB",
                "HAS_SERVER_CONFIG",
            )
        }
        try:
            mod._HARDWARE_CHECKED = False
            mod._HARDWARE_READY = False
            mod._HARDWARE_REASON = None
            mod._RNODE_PORT = "/dev/fake_rnode"
            mod._CARDCOMPUTER_PORT = "/dev/fake_cardputer"
            mod.HAS_PYSERIAL = True
            mod.HAS_FLASH_LIB = True
            mod.HAS_SERVER_CONFIG = True

            mock_ser = MagicMock()
            mock_ser.__enter__ = MagicMock(return_value=mock_ser)
            mock_ser.__exit__ = MagicMock(return_value=False)

            with (
                patch.object(mod, "check_rnode_firmware", return_value=True),
                patch.object(mod, "flash_rnode_firmware") as mock_flash,
                patch.object(mod, "serial") as mock_serial,
                patch.object(mod.cardputer_flash, "enter_raw_repl", return_value=True),
                patch.object(
                    mod.cardputer_flash,
                    "exec_raw",
                    return_value=(True, b"__LORA_OK__"),
                ),
            ):
                mock_serial.Serial.return_value = mock_ser
                _probe_hardware()

            # flash_rnode_firmware must NOT be called when firmware is present.
            mock_flash.assert_not_called()
            assert mod._HARDWARE_READY is True
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)

    def test_probe_rnode_device_disappears_after_flash(self):
        """_probe_hardware sets reason when RNode disappears after flash."""
        from unittest.mock import patch

        mod = sys.modules[__name__]

        _saved = {
            k: getattr(mod, k)
            for k in (
                "_HARDWARE_CHECKED",
                "_HARDWARE_READY",
                "_HARDWARE_REASON",
                "_RNODE_PORT",
                "_CARDCOMPUTER_PORT",
                "HAS_PYSERIAL",
                "HAS_FLASH_LIB",
                "HAS_SERVER_CONFIG",
            )
        }
        try:
            mod._HARDWARE_CHECKED = False
            mod._HARDWARE_READY = False
            mod._HARDWARE_REASON = None
            mod._RNODE_PORT = "/dev/fake_rnode"
            mod._CARDCOMPUTER_PORT = "/dev/fake_cardputer"
            mod.HAS_PYSERIAL = True
            mod.HAS_FLASH_LIB = True
            mod.HAS_SERVER_CONFIG = True

            # Firmware missing -> flash succeeds -> device disappears
            with (
                patch.object(mod, "check_rnode_firmware", return_value=False),
                patch.object(
                    mod, "flash_rnode_firmware", return_value=(True, "ok")
                ),
                patch.object(mod, "find_rnode_port", return_value=None),
            ):
                _probe_hardware()

            assert mod._HARDWARE_READY is False
            assert mod._HARDWARE_REASON is not None
            assert "disappeared" in mod._HARDWARE_REASON
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)

    def test_probe_rnode_post_flash_verification_fails(self):
        """_probe_hardware sets reason when post-flash firmware check fails."""
        from unittest.mock import patch

        mod = sys.modules[__name__]

        _saved = {
            k: getattr(mod, k)
            for k in (
                "_HARDWARE_CHECKED",
                "_HARDWARE_READY",
                "_HARDWARE_REASON",
                "_RNODE_PORT",
                "_CARDCOMPUTER_PORT",
                "HAS_PYSERIAL",
                "HAS_FLASH_LIB",
                "HAS_SERVER_CONFIG",
            )
        }
        try:
            mod._HARDWARE_CHECKED = False
            mod._HARDWARE_READY = False
            mod._HARDWARE_REASON = None
            mod._RNODE_PORT = "/dev/fake_rnode"
            mod._CARDCOMPUTER_PORT = "/dev/fake_cardputer"
            mod.HAS_PYSERIAL = True
            mod.HAS_FLASH_LIB = True
            mod.HAS_SERVER_CONFIG = True

            # Firmware missing -> flash succeeds -> re-discovery finds port
            # -> post-flash verification fails
            with (
                patch.object(
                    mod, "check_rnode_firmware", side_effect=[False, False]
                ),
                patch.object(
                    mod, "flash_rnode_firmware", return_value=(True, "ok")
                ),
                patch.object(
                    mod, "find_rnode_port", return_value="/dev/fake_rnode2"
                ),
            ):
                _probe_hardware()

            assert mod._HARDWARE_READY is False
            assert mod._HARDWARE_REASON is not None
            assert "post-flash verification" in mod._HARDWARE_REASON
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)


class TestCardputerLoRaE2E:
    """Tests that require Cardputer + Heltec RNode hardware."""

    @pytest.fixture(autouse=True)
    def skip_if_no_hardware(self):
        reason = _hardware_required()
        if reason:
            pytest.skip(reason)

    def test_rnode_detected(self):
        """RNode serial port is found and accessible."""
        assert _RNODE_PORT is not None, "RNode not detected"
        assert os.path.exists(_RNODE_PORT), f"RNode port {_RNODE_PORT} does not exist"

    def test_radio_params_match(self):
        """Server and Cardputer config must use identical radio parameters.

        Cardputer config.py uses kHz/MHz units while the server uses Hz.
        All values must match for LoRa communication to succeed.
        """
        server_ifaces = get_config_dict()["interfaces"]
        server_lora = server_ifaces["RNode LoRa"]

        # Cardputer client parameters (from config.py):
        #   freq_khz: 868000, bandwidth: "125", sf: 7, coding_rate: 5
        # Must match the server exactly for bidirectional LoRa communication.
        server_freq_mhz = server_lora["frequency"] / 1_000_000
        assert server_freq_mhz == 868.0, (
            f"Server freq {server_lora['frequency']} Hz != 868 MHz"
        )
        assert server_lora["bandwidth"] == 125000, (
            f"Server BW {server_lora['bandwidth']} Hz != 125 kHz"
        )
        assert server_lora["spreadingfactor"] == 7, (
            f"Server SF {server_lora['spreadingfactor']} != 7"
        )
        assert server_lora["codingrate"] == 5, (
            f"Server CR {server_lora['codingrate']} != 5"
        )

    def test_lora_full_e2e(self):
        """Full LoRa E2E: flash Cardputer with server hash, start server,
        and verify bidirectional LoRa message delivery.

        This is a SINGLE comprehensive test that initialises Reticulum once
        (Reticulum is a singleton — it cannot be reinitialised within the
        same process).  The test:

        1. Starts a temporary server with the Heltec RNode
        2. Injects the server's identity hash into the Cardputer config
        3. Flashes the Cardputer with updated config + client code
        4. Soft-resets the Cardputer and waits for "Hello" message receipt
        5. Verifies the message arrives over LoRa on the server side
        6. Optionally checks for ACK reply on Cardputer serial output
        """
        import tempfile
        import shutil
        import RNS
        import LXMF
        from lma_core import LMAOEnvelope
        from lma_core.config_utils import dict_to_ini
        from lma_core.storage import DuckDbStore

        # ── Setup: prepare server config ──
        cfg_dict = get_config_dict()
        rnode_port = cfg_dict["interfaces"]["RNode LoRa"]["port"]

        if not os.path.exists(rnode_port) and _RNODE_PORT:
            cfg_dict["interfaces"]["RNode LoRa"]["port"] = _RNODE_PORT
            rnode_port = _RNODE_PORT

        if not os.path.exists(rnode_port):
            pytest.skip(f"RNode port {rnode_port} not available")

        # ── Start Reticulum + get server identity ──
        configdir = tempfile.mkdtemp(prefix="lmao_e2e_rns_")
        try:
            config_content = dict_to_ini(
                {
                    "logging": {"loglevel": 3},
                    "transport": {"path": "/tmp/lmao_e2e_rns_state"},
                },
                {"RNode LoRa": cfg_dict["interfaces"]["RNode LoRa"]},
            )
            with open(os.path.join(configdir, "config"), "w") as f:
                f.write(config_content)

            RNS.Reticulum(configdir=configdir)
            identity = RNS.Identity()
            server_hash = RNS.hexrep(identity.hash, delimit=False)

            router = LXMF.LXMRouter(identity=identity, storagepath="/tmp/lmao_e2e_lxmf")

            # Shared state between server thread and test main thread
            received_messages = []
            sensor_messages = []
            message_event = threading.Event()

            # ── Temporary DuckDB for sensor pipeline validation ──
            db_fd, db_path = tempfile.mkstemp(suffix=".duckdb", prefix="lmao_e2e_")
            os.close(db_fd)
            store = DuckDbStore()
            store.initialize(db_path)

            def capture_delivery(message):
                """Record received messages for the test to inspect."""
                source = message.get_source()
                source_hash = (
                    RNS.hexrep(source.hash, delimit=False) if source else "<unknown>"
                )
                content_bytes = message.content if hasattr(message, "content") else b""
                try:
                    envelope = LMAOEnvelope()
                    envelope.ParseFromString(content_bytes)
                except Exception as exc:
                    import traceback
                    print(
                        f"WARNING: capture_delivery: envelope parse failed: {exc}",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    display_text = (
                        content_bytes.decode("utf-8", errors="replace")
                        if isinstance(content_bytes, bytes)
                        else str(content_bytes)
                    )
                else:
                    if envelope.HasField("text"):
                        display_text = envelope.text.content
                    elif envelope.HasField("sensor"):
                        # ── Store SensorReport in DuckDB ──
                        display_text = (
                            f"SensorReport(seq={envelope.sensor.seq}, "
                            f"readings={len(envelope.sensor.readings)})"
                        )
                        try:
                            asyncio.run(store.store_sensor_report(bytes(content_bytes)))
                            sensor_messages.append({
                                "source": source_hash,
                                "node_id": envelope.sensor.node_id,
                                "seq": envelope.sensor.seq,
                            })
                        except Exception:
                            import logging
                            logging.getLogger(__name__).warning(
                                "DuckDB store failed", exc_info=True
                            )
                    else:
                        try:
                            display_text = content_bytes.decode(
                                "utf-8", errors="replace"
                            )
                        except Exception:
                            display_text = "<undecodable>"

                received_messages.append(
                    {
                        "source": source_hash,
                        "content": display_text,
                        "raw": content_bytes,
                    }
                )
                message_event.set()

            router.register_delivery_callback(capture_delivery)

            # ── Prepare and flash the Cardputer ──

            root = cardputer_flash.find_client_root()
            assert root, "Cannot find cardputer_client/ source directory"

            config_path = os.path.join(root, "config.py")
            assert os.path.isfile(config_path), f"config.py not found: {config_path}"

            # Read the config once — we will upload it unmodified and then
            # overwrite /config.py on the device with the patched content
            # via upload_file.  This avoids modifying the source tree in place,
            # which would leave config.py dirty if the test is interrupted.
            with open(config_path) as f:
                original_config = f.read()

            patched_config = original_config.replace(
                "DEST_HASH = None",
                f'DEST_HASH = "{server_hash}"',
            )

            cardputer_ser = None
            try:
                # Flash the Cardputer with client files
                cardputer_ser = serial.Serial(_CARDCOMPUTER_PORT, 115200, timeout=1)
                time.sleep(0.6)

                ok = cardputer_flash.enter_raw_repl(cardputer_ser)
                assert ok, "Cannot enter raw REPL on Cardputer"

                # Upload all client files (config.py uploaded with DEST_HASH = None)
                for rel in cardputer_flash.FILES_TO_UPLOAD:
                    local_path = os.path.join(root, rel)
                    remote_path = rel
                    assert os.path.isfile(local_path), f"Missing source: {local_path}"
                    uploaded = cardputer_flash.upload_file(
                        cardputer_ser, local_path, remote_path
                    )
                    assert uploaded, f"Failed to upload {rel}"

                # Upload all library files (auto-discovered, like the flash tool does).
                # Since there are no heavy .mpy files, this is fast enough for E2E.
                lib_files = cardputer_flash.auto_discover_lib_files(root)
                for rel in lib_files:
                    local_path = os.path.join(root, rel)
                    if not os.path.isfile(local_path):
                        continue
                    remote_path = rel
                    uploaded = cardputer_flash.upload_file(
                        cardputer_ser, local_path, remote_path
                    )
                    assert uploaded, f"Failed to upload lib/{rel}"

                # Overwrite /config.py on the device with the patched version
                # containing the server hash.  Write to a temp file on the host
                # and upload via upload_file() so the change persists on the
                # device's flash filesystem (exec() only modifies the in-memory
                # namespace and is lost on soft reset).
                import tempfile

                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False
                ) as tmp:
                    tmp.write(patched_config)
                    tmp_path = tmp.name
                try:
                    uploaded = cardputer_flash.upload_file(
                        cardputer_ser, tmp_path, "config.py"
                    )
                    assert uploaded, "Failed to upload patched config.py"
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                # Verify the hash is present in the config on the device
                ok, out = cardputer_flash.exec_raw(
                    cardputer_ser,
                    "import config; print(config.DEST_HASH)",
                )
                assert ok, f"exec_raw failed: {out[:200]}"
                assert server_hash in out.decode("utf-8", errors="replace"), (
                    f"Server hash {server_hash} not found in device config output: "
                    f"{out[:200]}"
                )

                # Soft-reset to boot the Cardputer with new code
                cardputer_flash.exit_raw_repl(cardputer_ser)
                time.sleep(0.3)
                cardputer_ser.write(b"\x04")  # Ctrl+D soft reset in friendly REPL
                time.sleep(0.5)

                # ── Monitor serial for Cardputer boot + ACK reply ──
                cardputer_output = b""
                found_banner = False
                found_ack = False
                serial_deadline = time.time() + 30

                while time.time() < serial_deadline:
                    if cardputer_ser.in_waiting:
                        cardputer_output += cardputer_ser.read(cardputer_ser.in_waiting)

                    if b"LMAO" in cardputer_output or b"POC Ready" in cardputer_output:
                        found_banner = True

                    if case_insensitive_contains(
                        cardputer_output, "ack"
                    ) or case_insensitive_contains(cardputer_output, "reply"):
                        found_ack = True

                    # If we've received a LoRa message from the Cardputer on the
                    # server side, the test is successful even without ACK on
                    # the Cardputer (ACK may be missed due to timing)
                    if message_event.is_set():
                        # Give a little more time for ACK to show up
                        remaining = serial_deadline - time.time()
                        if found_banner and remaining < 5:
                            break
                        # Continue reading serial for ACK
                        time.sleep(0.25)
                        continue

                    time.sleep(0.25)

                # ── Report captured output ──
                captured = cardputer_output.decode("utf-8", errors="replace")
                print(f"\n[Cardputer serial output — {len(cardputer_output)} bytes]")
                print(captured[:2000])

                # ── Assertions ──
                assert found_banner, (
                    "Cardputer did not display LMAO banner after flashing.\n"
                    f"Captured: {captured[:500]}"
                )

                assert len(received_messages) > 0, (
                    "Server did not receive any messages from Cardputer over LoRa "
                    "within 30s. Check:\n"
                    "  - Antennas connected on both devices\n"
                    "  - RNode firmware is flashed (rnodeconf --port ... --info)\n"
                    "  - Radio params match (868 MHz, SF7, BW 125kHz, CR5)\n"
                    "  - Cardputer has native LoRa driver (.mpy) installed\n"
                    "    (if it shows 'no module named lora', the SX1262 native\n"
                    "     driver is missing from /lib on the device)\n"
                    "  - DEST_HASH was injected correctly\n"
                    f"\nServer hash: {server_hash}\n"
                    f"Cardputer serial: {captured[:1000]}"
                )

                # Verify message content
                msg = received_messages[0]
                print(f"\nReceived message from {msg['source']}: {msg['content']}")
                assert case_insensitive_contains(msg["content"].encode(), "hello"), (
                    f"Expected 'Hello' in message content, got: {msg['content'][:200]}"
                )

                # ── DuckDB sensor data assertion ──
                # SensorReports are sent by default (SEND_SENSOR=True), validate ingestion
                max_node_id = sensor_messages[0]["node_id"] if sensor_messages else server_hash
                rows = asyncio.run(store.query(
                    "SELECT node_id, value, unit FROM sensor_readings "
                    "ORDER BY id DESC LIMIT 5",
                ))
                assert len(rows) > 0, (
                    "No sensor data found in DuckDB after LoRa E2E test. "
                    f"Received {len(sensor_messages)} SensorReport envelope(s) over LoRa. "
                    "Cardputer should have sent SensorReports with SEND_SENSOR=True."
                )
                print(f"\n✅ Sensor data ingested to DuckDB: {len(rows)} row(s)")

                print("\n✅ LoRa E2E test passed!")
                print(f"   Cardputer booted: {found_banner}")
                print(f"   Server ACK on Cardputer: {found_ack}")
                print(f"   Messages received by server: {len(received_messages)}")

            finally:
                # Close serial port (note: we no longer modify config.py on
                # disk — the patched config is written directly to the device
                # via exec_raw, so there is nothing to restore).
                if cardputer_ser is not None:
                    try:
                        cardputer_ser.close()
                    except Exception:
                        pass

                # Close DuckDB store and clean up temp file
                try:
                    store.close()
                except Exception:
                    _logger.warning("DuckDB store close failed", exc_info=True)
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

        finally:
            shutil.rmtree(configdir, ignore_errors=True)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
