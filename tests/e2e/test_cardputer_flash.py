"""E2E test for Cardputer flashing and basic boot validation.

Requires physical M5Stack Cardputer hardware connected via USB.
All tests are skipped gracefully when hardware is not detected.

Covers:
  * Cardputer USB serial detection
  * MicroPython raw-REPL communication
  * File upload (config.py, main.py, proto/lma_encoder.py)
  * Post-flash soft-reset and boot-message validation

Run with::

    bazel test //tests:test_cardputer_e2e --test_output=all
"""

import os
import sys
import time

import pytest

try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False


# Import the flash library (available when running under Bazel via deps)
try:
    from cardputer_client import flash as cardputer_flash
    HAS_FLASH_LIB = True
except ImportError:
    HAS_FLASH_LIB = False


# ── helpers ─────────────────────────────────────────────────────────

def _find_port():
    """Return the device path of a connected Cardputer, or *None*."""
    if not HAS_PYSERIAL:
        return None
    return cardputer_flash.find_cardputer_port()


# Resolve hardware presence once at collection time so skips are fast.
_CARDCOMPUTER_PORT = _find_port() if HAS_FLASH_LIB and HAS_PYSERIAL else None
_HARDWARE_CHECKED = False
_HARDWARE_READY = False
_HARDWARE_REASON = None


def _probe_hardware():
    """Open the detected port and verify MicroPython raw REPL is available.

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
    if _CARDCOMPUTER_PORT is None:
        _HARDWARE_REASON = "Cardputer not detected — is it connected via USB?"
        return

    # Try opening the port and entering raw REPL to verify MicroPython
    try:
        with serial.Serial(_CARDCOMPUTER_PORT, 115200, timeout=1) as ser:
            time.sleep(0.6)
            ok = cardputer_flash.enter_raw_repl(ser)
            if ok:
                _HARDWARE_READY = True
            else:
                _HARDWARE_REASON = (
                    f"Device at {_CARDCOMPUTER_PORT} does not respond to MicroPython "
                    "raw REPL. Is the Cardputer running MicroPython?"
                )
    except Exception as exc:
        _HARDWARE_REASON = f"Cannot communicate with {_CARDCOMPUTER_PORT}: {exc}"


def _hardware_required():
    """Return a pytest skip marker string when hardware is missing."""
    _probe_hardware()
    return _HARDWARE_REASON


# ── tests ───────────────────────────────────────────────────────────

class TestHardwareDetection:
    """Tests that do NOT require a physical Cardputer."""

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
        """Every file in FILES_TO_UPLOAD must exist on disk."""
        root = cardputer_flash.find_client_root()
        assert root, "Cannot find cardputer_client/"
        for rel in cardputer_flash.FILES_TO_UPLOAD:
            full = os.path.join(root, rel)
            assert os.path.isfile(full), f"Missing: {full}"


class TestCardputerE2E:
    """Tests that require a physical Cardputer connected via USB."""

    @pytest.fixture(autouse=True)
    def skip_if_no_hardware(self):
        reason = _hardware_required()
        if reason:
            pytest.skip(reason)

    @pytest.fixture
    def serial_conn(self):
        """Open and yield a serial connection to the Cardputer."""
        ser = serial.Serial(_CARDCOMPUTER_PORT, 115200, timeout=1)
        time.sleep(0.6)
        try:
            yield ser
        finally:
            ser.close()

    def test_enter_raw_repl(self, serial_conn):
        """Can enter MicroPython raw REPL on the connected device."""
        assert cardputer_flash.enter_raw_repl(serial_conn), (
            "Cannot enter raw REPL. Is MicroPython running on the device?"
        )

    def test_verify_device(self, serial_conn):
        """Device reports as an ESP32 platform."""
        ok = cardputer_flash.enter_raw_repl(serial_conn)
        assert ok, "Cannot enter raw REPL"

        ok, info = cardputer_flash.verify_device(serial_conn)
        assert ok, f"Device verification failed: {info}"
        assert "esp32" in info.lower(), f"Not an ESP32: {info}"

        cardputer_flash.exit_raw_repl(serial_conn)

    def test_exec_raw_returns_output(self, serial_conn):
        """exec_raw communicates with the device and returns output."""
        ok = cardputer_flash.enter_raw_repl(serial_conn)
        assert ok, "Cannot enter raw REPL"

        # Simple expression that must return successfully
        ok, out = cardputer_flash.exec_raw(serial_conn, "print('hello')")
        assert ok, f"exec_raw failed: {out[:200]}"
        assert "hello" in out

        cardputer_flash.exit_raw_repl(serial_conn)

    def test_upload_small_file(self, serial_conn):
        """Upload a tiny file and verify it was written on the device."""
        ok = cardputer_flash.enter_raw_repl(serial_conn)
        assert ok, "Cannot enter raw REPL"

        # Write a small temporary file to the device
        ok, out = cardputer_flash.exec_raw(serial_conn, """
try:
    import os as _os
    _os.remove('/__e2e_test__.py')
except:
    pass
_f = open('/__e2e_test__.py', 'w')
_f.write('ANSWER = 42\\n')
_f.close()
print('WRITE_OK')
""")
        assert ok and "WRITE_OK" in out, f"Write failed: {out[:200]}"

        # Read back the file
        ok, out = cardputer_flash.exec_raw(serial_conn, """
try:
    _f = open('/__e2e_test__.py', 'r')
    print(_f.read())
    _f.close()
except Exception as _e:
    print('READ_ERR:' + str(_e))
""")
        assert ok and "ANSWER = 42" in out, f"Read-back failed: {out[:200]}"

        # Clean up
        cardputer_flash.exec_raw(serial_conn, """
try:
    import os as _os
    _os.remove('/__e2e_test__.py')
except:
    pass
""")

        cardputer_flash.exit_raw_repl(serial_conn)

    def test_flash_and_boot(self, serial_conn):
        """Full flash cycle: upload all client files, soft-reset, validate banner.

        This is the principal E2E test.  It uploads the three MicroPython
        files that constitute the Cardputer client, performs a soft reset,
        and reads serial output for the expected ``LMAO`` boot banner.
        """
        ok = cardputer_flash.enter_raw_repl(serial_conn)
        assert ok, "Cannot enter raw REPL"

        # Locate source files
        root = cardputer_flash.find_client_root()
        assert root, "Cannot find cardputer_client/ source directory"

        # Upload each file
        for rel in cardputer_flash.FILES_TO_UPLOAD:
            local_path = os.path.join(root, rel)
            remote_path = rel
            assert os.path.isfile(local_path), f"Missing source: {local_path}"
            uploaded = cardputer_flash.upload_file(serial_conn, local_path, remote_path)
            assert uploaded, f"Failed to upload {rel}"

        # Exit raw REPL and soft-reset
        cardputer_flash.exit_raw_repl(serial_conn)
        serial_conn.write(b"\x04")  # Ctrl+D soft reset in friendly REPL

        # Read boot output for up to 15 seconds, looking for the LMAO banner
        boot_output = b""
        deadline = time.time() + 15
        found_banner = False
        while time.time() < deadline:
            if serial_conn.in_waiting:
                boot_output += serial_conn.read(serial_conn.in_waiting)
            if b"LMAO" in boot_output or b"POC Ready" in boot_output:
                found_banner = True
                break
            time.sleep(0.25)

        # Print captured output for diagnostics
        captured = boot_output.decode("utf-8", errors="replace")
        print(f"\n[device boot output — {len(boot_output)} bytes]\n{captured[:1000]}")

        assert found_banner, (
            f"Device did not display LMAO banner after flashing.\n"
            f"Captured output ({len(boot_output)} B): {captured[:500]}"
        )


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
