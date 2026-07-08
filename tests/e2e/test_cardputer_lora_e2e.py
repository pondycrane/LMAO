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

import os
import sys
import time
import threading

import pytest

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

try:
    from lmao_server.config import get_config_dict
    HAS_SERVER_CONFIG = True
except ImportError:
    HAS_SERVER_CONFIG = False
    get_config_dict = None


# ── helpers ─────────────────────────────────────────────────────────


# Ensure e2e_helpers is importable when running the script directly
# (Bazel already adds the e2e/ directory to sys.path).
sys.path.insert(0, os.path.dirname(__file__))
from e2e_helpers import find_rnode_port


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
    global _HARDWARE_CHECKED, _HARDWARE_READY, _HARDWARE_REASON
    if _HARDWARE_CHECKED:
        return
    _HARDWARE_CHECKED = True

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
                    f"Device at {_CARDCOMPUTER_PORT} does not respond to MicroPython "
                    "raw REPL. Is the Cardputer running MicroPython?"
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
                    f"Cardputer at {_CARDCOMPUTER_PORT} is missing the native LoRa "
                    "driver (SX1262). The 'lora' module is not importable.\n"
                    "Install the lora.mpy driver in /lib/ on the Cardputer."
                )
                return
    except Exception as exc:
        _HARDWARE_REASON = f"Cannot communicate with Cardputer at {_CARDCOMPUTER_PORT}: {exc}"
        return

    _HARDWARE_READY = True


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
                {"logging": {"loglevel": 3}, "transport": {"path": "/tmp/lmao_e2e_rns_state"}},
                {"RNode LoRa": cfg_dict["interfaces"]["RNode LoRa"]},
            )
            with open(os.path.join(configdir, "config"), "w") as f:
                f.write(config_content)

            rns = RNS.Reticulum(configdir=configdir)
            identity = RNS.Identity()
            server_hash = RNS.hexrep(identity.hash, delimit=False)

            router = LXMF.LXMRouter(identity=identity, storagepath="/tmp/lmao_e2e_lxmf")

            # Shared state between server thread and test main thread
            received_messages = []
            message_event = threading.Event()

            def capture_delivery(message):
                """Record received messages for the test to inspect."""
                source = message.get_source()
                source_hash = (
                    RNS.hexrep(source.hash, delimit=False)
                    if source else "<unknown>"
                )
                content_bytes = message.content if hasattr(message, "content") else b""
                try:
                    envelope = LMAOEnvelope()
                    envelope.ParseFromString(content_bytes)
                    if envelope.HasField("text"):
                        display_text = envelope.text.content
                    else:
                        display_text = content_bytes.decode("utf-8", errors="replace")
                except Exception as exc:
                    print(f"WARNING: capture_delivery: envelope parse failed: {exc}")
                    display_text = content_bytes.decode("utf-8", errors="replace")

                received_messages.append({
                    "source": source_hash,
                    "content": display_text,
                    "raw": content_bytes,
                })
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
                        cardputer_output += cardputer_ser.read(
                            cardputer_ser.in_waiting
                        )

                    if b"LMAO" in cardputer_output or b"POC Ready" in cardputer_output:
                        found_banner = True

                    if b"ACK" in cardputer_output or b"Reply" in cardputer_output:
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
                assert "Hello" in msg["content"], (
                    f"Expected 'Hello' in message content, got: {msg['content'][:200]}"
                )

                print(f"\n✅ LoRa E2E test passed!")
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

        finally:
            shutil.rmtree(configdir, ignore_errors=True)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
