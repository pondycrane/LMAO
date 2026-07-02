"""Software-mock tests for cardputer_client.flash — no hardware required.

These tests cover CLI error paths, port detection heuristics, and edge
cases that are only exercised by manual hardware testing in the E2E suite.

Run with::

    bazel test //tests:test_flash --test_output=all
"""

import builtins
import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Import the flash library (available when running under Bazel via deps)
try:
    from cardputer_client import flash as cardputer_flash
except ImportError:
    cardputer_flash = None


# ── helpers ─────────────────────────────────────────────────────────

def _make_port(device, vid, description):
    """Build a lightweight fake port object for mocking list_ports."""
    # pyserial returns list_port_info namedtuples — we simulate with SimpleNamespace
    return SimpleNamespace(device=device, vid=vid, description=description)


# ── Auto-discover lib files ──────────────────────────────────────────

class TestAutoDiscoverLibFiles:
    """Tests for auto_discover_lib_files() — mocked os.walk."""

    def test_discovers_py_and_mpy_files(self):
        """Returns sorted list of .py and .mpy files relative to client_root."""
        fake_root = "/tmp/client"
        mock_walk = [
            ("/tmp/client/lib", ["urns"], ["__init__.py", "README.txt"]),
            ("/tmp/client/lib/urns", ["crypto"], ["__init__.py", "reticulum.py"]),
            ("/tmp/client/lib/urns/crypto", [], ["ed25519.py", "aes.py", "speed_test.mpy"]),
        ]
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=mock_walk):
            result = cardputer_flash.auto_discover_lib_files(fake_root)

        expected = [
            "lib/__init__.py",
            "lib/urns/__init__.py",
            "lib/urns/crypto/aes.py",
            "lib/urns/crypto/ed25519.py",
            "lib/urns/crypto/speed_test.mpy",
            "lib/urns/reticulum.py",
        ]
        assert result == expected

    def test_skips_non_py_mpy_files(self):
        """Only .py and .mpy files are included."""
        fake_root = "/tmp/client"
        mock_walk = [
            ("/tmp/client/lib", [], [".DS_Store", "README.md", "config.json", "main.py"]),
        ]
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=mock_walk):
            result = cardputer_flash.auto_discover_lib_files(fake_root)
        assert result == ["lib/main.py"]

    def test_returns_empty_list_when_no_files(self):
        """Returns empty list when lib/ directory does not contain any files."""
        fake_root = "/tmp/client"
        mock_walk = [
            ("/tmp/client/lib", [], []),
        ]
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=mock_walk):
            result = cardputer_flash.auto_discover_lib_files(fake_root)
        assert result == []

    def test_returns_sorted_results(self):
        """Results are sorted alphabetically regardless of os.walk order."""
        fake_root = "/tmp/client"
        mock_walk = [
            ("/tmp/client/lib", [], ["z.py", "a.py", "m.py"]),
        ]
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk", return_value=mock_walk):
            result = cardputer_flash.auto_discover_lib_files(fake_root)
        assert result == ["lib/a.py", "lib/m.py", "lib/z.py"]

    def test_returns_empty_list_when_lib_dir_missing(self, capsys):
        """Returns empty list and prints warning when lib/ directory doesn't exist."""
        fake_root = "/tmp/client_without_lib"
        with patch("os.path.isdir", return_value=False):
            result = cardputer_flash.auto_discover_lib_files(fake_root)
        assert result == []
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "Library directory not found" in captured.out


# ── Path sanitization ───────────────────────────────────────────────

class TestSanitizePath:
    """Tests for _sanitize_path_for_script()."""

    def test_normal_path_passes_through(self):
        assert cardputer_flash._sanitize_path_for_script("/foo/bar.py") == "/foo/bar.py"

    def test_single_quote_is_escaped(self):
        result = cardputer_flash._sanitize_path_for_script("/foo'bar.py")
        assert result == "/foo\\'bar.py"

    def test_multiple_single_quotes_escaped(self):
        result = cardputer_flash._sanitize_path_for_script("/a'b'c.py")
        assert result == "/a\\'b\\'c.py"

    def test_backslash_raises(self):
        with pytest.raises(ValueError, match="backslash"):
            cardputer_flash._sanitize_path_for_script("/foo\\bar")

    def test_non_printable_raises(self):
        with pytest.raises(ValueError, match="non-printable"):
            cardputer_flash._sanitize_path_for_script("/foo\x00bar")


# ── Port detection ──────────────────────────────────────────────────

class TestFindCardputerPort:
    """Tests for find_cardputer_port() heuristic logic."""

    def test_preferred_port_returned_without_scanning(self):
        """When preferred is given, return it immediately without scanning."""
        with patch("serial.tools.list_ports.comports") as mock_comports:
            result = cardputer_flash.find_cardputer_port(preferred="/dev/ttyS0")
        mock_comports.assert_not_called()
        assert result == "/dev/ttyS0"

    def test_matches_by_espressif_vid(self):
        """Espressif VID (0x303A) devices are matched."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x303A, "USB JTAG/serial debug unit"),
            _make_port("/dev/ttyUSB1", 0x0403, "FTDI FT232"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyUSB0"

    def test_fallback_by_description_keyword(self):
        """Devices without known VID fall back to keyword matching."""
        mock_ports = [
            _make_port("/dev/ttyS0", None, "Silicon Labs CP210x USB to UART Bridge"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyS0"

    def test_first_matching_port_wins(self):
        """When multiple ports match, the first one encountered is returned."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x0403, "ESP32 CP210x USB UART"),
            _make_port("/dev/ttyACM0", 0x303A, "USB JTAG/serial debug unit"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        # The first port matches by keyword 'esp32' before the VID port is reached
        assert result == "/dev/ttyUSB0"

    def test_vid_match_wins_when_listed_first(self):
        """When a VID=0x303A port is listed first, it wins."""
        mock_ports = [
            _make_port("/dev/ttyACM0", 0x303A, "USB JTAG/serial debug unit"),
            _make_port("/dev/ttyUSB0", 0x0403, "ESP32 CP210x USB UART"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyACM0"

    def test_matches_ch340_by_keyword(self):
        """CH340 devices are matched via keyword."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x1A86, "USB Serial CH340"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyUSB0"

    def test_matches_usb_serial_by_keyword(self):
        """'usb serial' in description triggers a match."""
        mock_ports = [
            _make_port("/dev/ttyACM0", 0x2341, "USB Serial Device"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyACM0"

    def test_matches_jtag_by_keyword(self):
        """'jtag' in description triggers a match."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x10C4, "USB JTAG Debug"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result == "/dev/ttyUSB0"

    def test_returns_none_when_no_match(self):
        """None is returned when no Cardputer-compatible port is found."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x0403, "FTDI FT232"),
            _make_port("/dev/ttyUSB1", 0x1A86, "CH341"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result is None

    def test_handles_none_vid_gracefully(self):
        """Ports with vid=None do not crash."""
        mock_ports = [
            _make_port("/dev/ttyS0", None, "Unknown device"),
            _make_port("/dev/ttyS1", None, "Serial port"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result is None

    def test_handles_missing_vid_attribute(self):
        """Ports without a 'vid' attribute do not crash the scanner."""
        # Use a class that does not have 'vid' at all
        class PortNoVid:
            device = "/dev/ttyS0"
            description = "No VID"

        mock_ports = [PortNoVid]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result is None  # no match, but no crash

    def test_handles_missing_description_attribute(self):
        """Ports without a 'description' attribute do not crash the scanner."""
        class PortNoDesc:
            device = "/dev/ttyS0"
            vid = None

        mock_ports = [PortNoDesc]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = cardputer_flash.find_cardputer_port()
        assert result is None  # no match, but no crash

    @pytest.mark.parametrize("preferred", [None, "/dev/ttyACM0", ""])
    def test_comports_exception_returns_none(self, preferred):
        """When comports() raises, return None with a warning."""
        with patch("serial.tools.list_ports.comports", side_effect=OSError("permission denied")):
            result = cardputer_flash.find_cardputer_port(preferred=preferred)
        if preferred:
            # If preferred is given, it's returned before comports() is called
            assert result == preferred
        else:
            assert result is None

    def test_comports_exception_when_no_preferred(self, capsys):
        """Warning message is printed when comports() fails."""
        with patch("serial.tools.list_ports.comports", side_effect=OSError("permission denied")):
            result = cardputer_flash.find_cardputer_port()
        assert result is None
        captured = capsys.readouterr()
        assert "Could not enumerate serial ports" in captured.out


# ── main() CLI error paths ──────────────────────────────────────────

class TestMain:
    """Software-mocked tests for flash.main() — no hardware required."""

    @pytest.fixture(autouse=True)
    def isolate_main(self, monkeypatch):
        """Reset sys.argv for each test so argparse doesn't see pytest args."""
        monkeypatch.setattr(sys, 'argv', ['flash'])

    @staticmethod
    def _set_argv(*args):
        """Override sys.argv with flash + given args."""
        sys.argv = ['flash'] + list(args)

    # ── Happy path tests ─────────────────────────────────────────

    def test_main_successful_flash(self, capsys):
        """main() completes successfully when everything works."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_cardputer_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(True, "ESP32 / esp32 / Cardputer")), \
             patch("cardputer_client.flash.upload_file", return_value=True), \
             patch("os.path.getsize", return_value=1234):
            cardputer_flash.main()

        captured = capsys.readouterr()
        assert "Flash complete" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_proceeds_with_empty_lib_files(self, capsys):
        """main() succeeds when auto_discover_lib_files returns empty list."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_cardputer_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(True, "ESP32 / esp32 / Cardputer")), \
             patch("cardputer_client.flash.upload_file", return_value=True), \
             patch("os.path.getsize", return_value=1234), \
             patch("cardputer_client.flash.auto_discover_lib_files", return_value=[]):
            cardputer_flash.main()

        captured = capsys.readouterr()
        assert "Flash complete" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_verify_only_exits_cleanly(self, capsys):
        """main() with --verify-only returns after verification without uploading."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_cardputer_client"
        mock_ser = MagicMock()

        self._set_argv("--verify-only")

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(True, "ESP32 / esp32 / Cardputer")):
            cardputer_flash.main()

        captured = capsys.readouterr()
        assert "Verification complete" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_continues_on_verify_failure(self, capsys):
        """main() warns but continues when device verification returns False."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_cardputer_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(False, "Not an ESP32 — platform='win32'")), \
             patch("cardputer_client.flash.upload_file", return_value=True), \
             patch("os.path.getsize", return_value=1234):
            cardputer_flash.main()

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "Proceeding anyway" in captured.out

    def test_main_with_explicit_port_passed(self, capsys):
        """main() with --port argument passes it to find_cardputer_port."""
        port = "/dev/ttyUSB1"
        fake_root = "/tmp/fake_cardputer_client"
        mock_ser = MagicMock()

        self._set_argv("--port", port)

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(True, "ESP32")), \
             patch("cardputer_client.flash.upload_file", return_value=True), \
             patch("os.path.getsize", return_value=1234):
            cardputer_flash.main()

        captured = capsys.readouterr()
        assert f"Connecting to Cardputer on {port}" in captured.out

    # ── Error exit path tests ────────────────────────────────────

    def test_main_exits_when_client_root_missing(self, capsys):
        """main() prints error and exits(1) when cardputer_client/ not found."""
        with patch("cardputer_client.flash.find_client_root", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Cannot find cardputer_client/" in captured.out

    def test_main_exits_when_required_file_missing(self, capsys):
        """main() exits(1) when a FILES_TO_UPLOAD entry doesn't exist."""
        fake_root = "/tmp/fake_client"

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_main_exits_when_no_cardputer_port_found(self, capsys):
        """main() lists detected ports and exits(1) when no Cardputer found."""
        fake_root = "/tmp/fake_client"

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=None), \
             patch("serial.tools.list_ports.comports", return_value=[]):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Could not find Cardputer" in captured.out

    def test_main_exits_on_serial_open_failure(self, capsys):
        """main() exits(1) when the serial port cannot be opened."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"

        try:
            import serial
            exc_cls = serial.SerialException
        except ImportError:
            exc_cls = OSError

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", side_effect=exc_cls("Permission denied")):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Cannot open" in captured.out
        assert "Permission denied" in captured.out

    def test_main_exits_on_raw_repl_entry_failure(self, capsys):
        """main() exits(1) when raw REPL entry fails."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Could not enter raw REPL" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_exits_on_upload_failure(self, capsys):
        """main() exits(1) when a file upload fails."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl", return_value=True), \
             patch("cardputer_client.flash.verify_device",
                   return_value=(True, "ESP32")), \
             patch("cardputer_client.flash.upload_file", return_value=False), \
             patch("os.path.getsize", return_value=0):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "FAILED" in captured.out

    def test_main_handles_keyboard_interrupt(self, capsys):
        """main() exits(1) gracefully on Ctrl+C."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl",
                   side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Aborted by user" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_handles_serial_exception(self, capsys):
        """main() exits(1) with a user-friendly message on SerialException."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        try:
            import serial
            exc_cls = serial.SerialException
        except ImportError:
            exc_cls = OSError

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl",
                   side_effect=exc_cls("device disconnected")):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Lost connection" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_handles_generic_exception(self, capsys):
        """main() catches unexpected exceptions and exits(1) with a message."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl",
                   side_effect=RuntimeError("something unexpected")):
            with pytest.raises(SystemExit) as exc:
                cardputer_flash.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Unexpected error" in captured.out
        mock_ser.close.assert_called_once()

    def test_main_always_closes_serial_port(self, capsys):
        """ser.close() is called in finally block even when an error occurs."""
        port = "/dev/ttyACM0"
        fake_root = "/tmp/fake_client"
        mock_ser = MagicMock()

        with patch("cardputer_client.flash.find_client_root", return_value=fake_root), \
             patch("os.path.isfile", return_value=True), \
             patch("cardputer_client.flash.find_cardputer_port", return_value=port), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("cardputer_client.flash.enter_raw_repl",
                   side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                cardputer_flash.main()
        mock_ser.close.assert_called_once()


# ── exec_raw edge cases ─────────────────────────────────────────────

class TestExecRaw:
    """Mocked tests for exec_raw() communication edge cases."""

    def test_exec_raw_timeout_returns_false(self):
        """When the device never sends \\x04>, exec_raw returns (False, reason)."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0  # no data ever available

        ok, out = cardputer_flash.exec_raw(mock_ser, "print('hello')", timeout=0.1)
        assert ok is False
        assert "Timeout" in out

    def test_exec_raw_detects_ok_in_output(self):
        """exec_raw returns True when 'OK' appears in the output."""
        mock_ser = MagicMock()
        # Simulate the device response in two reads
        mock_ser.in_waiting = 1
        mock_ser.read.side_effect = [
            b"echo of code\r\nOK\r\nhello\r\n\x04>",
        ]

        ok, out = cardputer_flash.exec_raw(mock_ser, "print('hello')")
        assert ok is True
        assert "hello" in out

    def test_exec_raw_handles_serial_exception(self):
        """exec_raw returns (False, reason) on SerialException."""
        try:
            import serial
            exc_cls = serial.SerialException
        except ImportError:
            exc_cls = OSError

        mock_ser = MagicMock()
        mock_ser.write.side_effect = exc_cls("disconnected")

        ok, out = cardputer_flash.exec_raw(mock_ser, "print('x')")
        assert ok is False
        assert "Serial communication error" in out


# ── enter_raw_repl edge cases ───────────────────────────────────────

class TestEnterRawRepl:
    """Mocked tests for enter_raw_repl() edge cases."""

    def test_timeout_returns_false(self):
        """enter_raw_repl returns False when the raw REPL banner never appears."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0

        result = cardputer_flash.enter_raw_repl(mock_ser)
        assert result is False

    def test_success_returns_true(self):
        """enter_raw_repl returns True when the banner is received."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 1
        mock_ser.read.return_value = b"raw REPL; CTRL-B to exit\r\n>"

        result = cardputer_flash.enter_raw_repl(mock_ser)
        assert result is True

    def test_serial_exception_returns_false(self):
        """enter_raw_repl returns False on SerialException."""
        try:
            import serial
            exc_cls = serial.SerialException
        except ImportError:
            exc_cls = OSError

        mock_ser = MagicMock()
        mock_ser.write.side_effect = exc_cls("disconnected")

        result = cardputer_flash.enter_raw_repl(mock_ser)
        assert result is False


# ── exit_raw_repl ───────────────────────────────────────────────────

class TestExitRawRepl:
    """Tests for exit_raw_repl() behavior."""

    def test_writes_correct_byte_sequence(self):
        """exit_raw_repl sends \\r\\x02 (Ctrl+B)."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0

        cardputer_flash.exit_raw_repl(mock_ser)
        mock_ser.write.assert_any_call(b"\r\x02")

    def test_drains_remaining_data(self):
        """exit_raw_repl drains any data in the buffer."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 5

        cardputer_flash.exit_raw_repl(mock_ser)
        mock_ser.read.assert_called_with(5)

    def test_serial_exception_is_silent(self):
        """exit_raw_repl silently swallows SerialException (device may be gone)."""
        try:
            import serial
            exc_cls = serial.SerialException
        except ImportError:
            exc_cls = OSError

        mock_ser = MagicMock()
        mock_ser.write.side_effect = exc_cls("disconnected")

        # Should not raise
        cardputer_flash.exit_raw_repl(mock_ser)


if __name__ == "__main__":
    import pytest as _pytest
    import sys as _sys
    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
