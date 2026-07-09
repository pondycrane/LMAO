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
from e2e_helpers import RNODE_VIDS, find_rnode_port


def _make_port(device, vid, description):
    """Build a lightweight fake port object for mocking list_ports."""
    return SimpleNamespace(device=device, vid=vid, description=description)


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


if __name__ == "__main__":
    import pytest as _pytest
    import sys as _sys

    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
