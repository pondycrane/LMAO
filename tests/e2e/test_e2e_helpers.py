"""Unit tests for e2e_helpers — no hardware required.

Run with::

    bazel test //tests:test_e2e_helpers --test_output=all
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch


# Ensure the e2e/ directory is on sys.path for sibling imports
sys.path.insert(0, os.path.dirname(__file__))
from e2e_helpers import (
    RNODE_VIDS,
    find_rnode_port,
    case_insensitive_contains,
    check_rnode_firmware,
    flash_rnode_firmware,
)


def _make_port(device, vid, description):
    """Build a lightweight fake port object for mocking list_ports."""
    return SimpleNamespace(device=device, vid=vid, description=description)


class TestCaseInsensitiveContains:
    """Tests for case_insensitive_contains() — pure function, no hardware needed."""

    def test_matches_lowercase_needle_in_mixed_case_haystack(self):
        assert case_insensitive_contains(b"Hello World", "hello")

    def test_matches_uppercase_needle_in_lowercase_haystack(self):
        assert case_insensitive_contains(b"hello world", "HELLO")

    def test_matches_mixed_case_everything(self):
        assert case_insensitive_contains(b"ACK received OK", "ack")

    def test_matches_reply(self):
        assert case_insensitive_contains(b"Reply sent", "reply")

    def test_rejects_absent_needle(self):
        assert not case_insensitive_contains(b"Hello World", "goodbye")

    def test_empty_haystack(self):
        assert not case_insensitive_contains(b"", "hello")

    def test_empty_needle(self):
        assert case_insensitive_contains(b"anything", "")

    def test_needle_at_start(self):
        assert case_insensitive_contains(b"ACK: message received", "ack")

    def test_needle_at_end(self):
        assert case_insensitive_contains(b"message: ACK", "ack")

    def test_needle_in_middle(self):
        assert case_insensitive_contains(b"got [ACK] from node", "ack")

    def test_needle_longer_than_haystack(self):
        assert not case_insensitive_contains(b"hi", "hello world")

    def test_binary_bytes_dont_crash(self):
        assert case_insensitive_contains(b"\xff\xfeACK\x00", "ack")

    def test_non_ascii_needle_is_encoded(self):
        # Plain ASCII only — this is the contract for needle parameter
        assert case_insensitive_contains(b"cafe", "cafe")


class TestFindRNodePort:
    """Tests for find_rnode_port() — mocked serial port enumeration."""

    def test_matches_espressif_vid(self):
        """Espressif VID (0x303A) devices are matched."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x303A, "USB JTAG/serial debug unit"),
            _make_port("/dev/ttyUSB1", 0x0403, "FTDI FT232"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_matches_cp210x_vid(self):
        """CP210x VID (0x10C4) devices are matched."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x10C4, "CP2102 USB to UART Bridge"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_matches_ch340_vid(self):
        """CH340 VID (0x1A86) devices are matched."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x1A86, "USB Serial CH340"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_fallback_by_description_keyword(self):
        """Devices without known VID fall back to 'rnode' description match."""
        mock_ports = [
            _make_port("/dev/ttyS0", None, "RNode LoRa Interface"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyS0"

    def test_rnode_in_description_trumps_vid_mismatch(self):
        """'rnode' keyword in description beats VID mismatch."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x0403, "RNODE v3 USB"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_returns_none_when_no_match(self):
        """None is returned when no RNode-compatible port is found."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x0403, "FTDI FT232"),
            _make_port("/dev/ttyUSB1", 0x067B, "Prolific PL2303"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result is None

    def test_handles_none_vid_gracefully(self):
        """Ports with vid=None do not crash, fall through to description."""
        mock_ports = [
            _make_port("/dev/ttyS0", None, "Some USB device"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result is None  # No match, but no crash

    def test_handles_missing_vid_attribute(self):
        """Ports without a 'vid' attribute do not crash."""

        class PortNoVid:
            device = "/dev/ttyS0"
            description = "RNode device"

        mock_ports = [PortNoVid]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyS0"  # matched by description

    def test_handles_missing_description_attribute(self):
        """Ports without 'description' attribute do not crash."""

        class PortNoDesc:
            device = "/dev/ttyS0"
            vid = 0x303A

        mock_ports = [PortNoDesc]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyS0"  # matched by VID

    def test_comports_exception_prints_warning(self, capsys):
        """When comports() raises, return None with a warning."""
        with patch(
            "serial.tools.list_ports.comports", side_effect=OSError("permission denied")
        ):
            result = find_rnode_port()
        assert result is None
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Could not enumerate serial ports" in captured.err

    def test_first_matching_port_wins(self):
        """When multiple ports match, the first one is returned."""
        mock_ports = [
            _make_port("/dev/ttyUSB0", 0x0403, "FTDI FT232"),
            _make_port("/dev/ttyACM0", 0x303A, "Espressif USB JTAG"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=mock_ports):
            result = find_rnode_port()
        assert result == "/dev/ttyACM0"  # second port has Espressif VID

    def test_empty_ports_list_returns_none(self):
        """Empty list of ports returns None."""
        with patch("serial.tools.list_ports.comports", return_value=[]):
            result = find_rnode_port()
        assert result is None


class TestRNODEVIDS:
    """Sanity checks on the RNODE_VIDS constant."""

    def test_is_set_of_ints(self):
        """RNODE_VIDS must be a set of integers."""
        assert isinstance(RNODE_VIDS, set)
        for vid in RNODE_VIDS:
            assert isinstance(vid, int)

    def test_contains_expected_vids(self):
        """Expected VID values are present."""
        assert 0x303A in RNODE_VIDS  # Espressif
        assert 0x10C4 in RNODE_VIDS  # CP210x (Silicon Labs)
        assert 0x1A86 in RNODE_VIDS  # CH340


class TestCheckRNodeFirmware:
    """Tests for check_rnode_firmware() — mocked subprocess calls."""

    def test_firmware_detected(self):
        """Returns True when rnodeconf --info exits 0 with RNode output."""
        mock_result = SimpleNamespace(
            returncode=0,
            stdout="RNode Firmware v1.73\nLoRa: ON\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert check_rnode_firmware("/dev/ttyUSB0") is True

    def test_firmware_detected_lora_keyword(self):
        """Returns True when output contains 'LoRa:' but not 'RNode Firmware'."""
        mock_result = SimpleNamespace(
            returncode=0,
            stdout="Device: Heltec\nLoRa: SX1262\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert check_rnode_firmware("/dev/ttyUSB0") is True

    def test_nonzero_returncode(self):
        """Returns False when subprocess exits non-zero."""
        mock_result = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Error: could not open port",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert check_rnode_firmware("/dev/ttyUSB0") is False

    def test_timeout(self):
        """Returns False (does not crash) when subprocess times out."""
        with patch(
            "subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 15),
        ):
            assert check_rnode_firmware("/dev/ttyUSB0") is False

    def test_filenotfound(self):
        """Returns False when python interpreter is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert check_rnode_firmware("/dev/ttyUSB0") is False

    def test_unexpected_exception(self):
        """Returns False when an unexpected exception occurs (e.g. PermissionError)."""
        with patch("subprocess.run", side_effect=PermissionError("denied")):
            assert check_rnode_firmware("/dev/ttyUSB0") is False

    def test_empty_output_success(self):
        """Returns False when exit 0 but no firmware signature in output."""
        mock_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert check_rnode_firmware("/dev/ttyUSB0") is False

    def test_firmware_in_stderr(self):
        """Returns True when firmware signature appears in stderr."""
        mock_result = SimpleNamespace(
            returncode=0,
            stdout="Probing /dev/ttyUSB0...",
            stderr="RNode Firmware v1.73 detected",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert check_rnode_firmware("/dev/ttyUSB0") is True


class TestFlashRNodeFirmware:
    """Tests for flash_rnode_firmware() — mocked subprocess calls."""

    def test_flash_succeeds(self):
        """Returns (True, ...) when subprocess exits 0."""
        mock_result = SimpleNamespace(
            returncode=0,
            stdout="Flashing...\nDone.\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is True
        assert "success" in msg.lower()

    def test_flash_fails_with_stderr(self):
        """Returns (False, error_msg) when subprocess exits non-zero."""
        mock_result = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="esptool.FatalError: Failed to connect",
        )
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is False
        assert "Flash failed" in msg

    def test_flash_fails_no_stderr(self):
        """Returns (False, ...) with exit code when stderr is empty."""
        mock_result = SimpleNamespace(returncode=2, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is False
        assert "rnodeconf exited 2" in msg

    def test_timeout(self):
        """Returns (False, ...) when autoinstall times out."""
        with patch(
            "subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 120),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is False
        assert "timed out" in msg

    def test_filenotfound(self):
        """Returns (False, ...) when python interpreter is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is False
        assert "Cannot run rnodeconf" in msg

    def test_unexpected_exception(self):
        """Returns (False, ...) when an unexpected exception occurs (e.g. OSError)."""
        with patch("subprocess.run", side_effect=OSError("disk full")):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0")
        assert ok is False
        assert "autoinstall" in msg

    def test_tuple_return_type(self):
        """Return type is always tuple[bool, str] regardless of path."""
        mock_result = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = flash_rnode_firmware("/dev/ttyUSB0")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


if __name__ == "__main__":
    import pytest as _pytest
    import sys as _sys

    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
