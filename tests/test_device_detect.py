"""Unit tests for lma_core.device_detect — no hardware required.

Covers VID/PID matching, product/manufacturer confirmation, the
top-level ``detect_devices()`` API, convenience helpers, and protocol
probes.  All serial I/O is mocked via ``unittest.mock`` — pyserial
does not need to be installed to run these tests.

Run with::

    bazel test //tests:test_device_detect --test_output=all
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Import the real device_detect module (available under Bazel via deps).
try:
    from lma_core import device_detect
except ImportError:
    device_detect = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fake port factories
# ---------------------------------------------------------------------------


def _make_port(
    device,
    vid=None,
    pid=None,
    product="",
    manufacturer="",
    serial_number="",
    description="",
):
    """Build a lightweight fake port object for mocking list_ports.comports()."""
    return SimpleNamespace(
        device=device,
        vid=vid,
        pid=pid,
        product=product,
        manufacturer=manufacturer,
        serial_number=serial_number,
        description=description,
    )


# Real device fingerprints for reference:
_CARDPUTER_PORT = _make_port(
    device="/dev/ttyACM0",
    vid=0x303A,
    pid=0x8120,
    product="M5Stack UiFlow 2.0",
    manufacturer="M5Stack Technology Co., Ltd",
    description="M5Stack Cardputer-ADV-Custom",
)

_RNODE_PORT = _make_port(
    device="/dev/ttyUSB0",
    vid=0x10C4,
    pid=0xEA60,
    product="CP2102 USB to UART Bridge Controller",
    manufacturer="Silicon Labs",
    serial_number="0001",
    description="CP2102 USB to UART Bridge Controller",
)


# ---------------------------------------------------------------------------
# Helper: mock comports
# ---------------------------------------------------------------------------


def _mock_comports(ports):
    """Patch serial.tools.list_ports.comports to return *ports*."""
    return patch("serial.tools.list_ports.comports", return_value=list(ports))


# ---------------------------------------------------------------------------
# _read_port_info
# ---------------------------------------------------------------------------


class TestReadPortInfo:
    """Tests for _read_port_info()."""

    def test_extracts_all_fields(self):
        p = _make_port(
            device="/dev/ttyUSB0",
            vid=0x10C4,
            pid=0xEA60,
            product="CP2102",
            manufacturer="Silicon Labs",
            serial_number="0001",
            description="desc",
        )
        info = device_detect._read_port_info(p)
        assert info.port == "/dev/ttyUSB0"
        assert info.vid == 0x10C4
        assert info.pid == 0xEA60
        assert info.product == "CP2102"
        assert info.manufacturer == "Silicon Labs"
        assert info.serial == "0001"
        assert info.description == "desc"

    def test_handles_missing_attributes(self):
        p = SimpleNamespace(device="/dev/ttyS0")
        info = device_detect._read_port_info(p)
        assert info.port == "/dev/ttyS0"
        assert info.vid is None
        assert info.pid is None
        assert info.product is None
        assert info.manufacturer is None
        assert info.serial is None
        assert info.description is None

    def test_handles_none_values(self):
        p = _make_port(device="/dev/ttyUSB0", vid=None, pid=None, product=None, manufacturer=None)
        info = device_detect._read_port_info(p)
        assert info.vid is None
        assert info.pid is None
        assert info.product is None
        assert info.manufacturer is None

    def test_empty_strings_become_none(self):
        p = _make_port(device="/dev/ttyUSB0", product="  ", manufacturer="")
        info = device_detect._read_port_info(p)
        assert info.product is None
        assert info.manufacturer is None


# ---------------------------------------------------------------------------
# _match_fingerprint
# ---------------------------------------------------------------------------


class TestMatchFingerprint:
    """Tests for _match_fingerprint()."""

    def test_cardputer_high_confidence(self):
        """Real Cardputer fingerprint → high confidence."""
        info = device_detect._read_port_info(_CARDPUTER_PORT)
        result = device_detect._match_fingerprint(
            info, device_detect._CARDCOMPUTER_FINGERPRINTS
        )
        assert result == "high"

    def test_rnode_high_confidence(self):
        """Real RNode fingerprint → high confidence."""
        info = device_detect._read_port_info(_RNODE_PORT)
        result = device_detect._match_fingerprint(info, device_detect._RNODE_FINGERPRINTS)
        assert result == "high"

    def test_no_match_returns_empty(self):
        """Unknown device returns empty string."""
        info = device_detect.DeviceInfo(port="/dev/ttyUSB1", vid=0xABCD, pid=0x1234)
        result = device_detect._match_fingerprint(
            info, device_detect._CARDCOMPUTER_FINGERPRINTS
        )
        assert result == ""

    def test_no_vid_pid_returns_empty(self):
        """Device without VID/PID returns empty."""
        info = device_detect.DeviceInfo(port="/dev/ttyS0", vid=None, pid=None)
        result = device_detect._match_fingerprint(
            info, device_detect._CARDCOMPUTER_FINGERPRINTS
        )
        assert result == ""

    def test_vid_pid_match_without_strings_high_confidence(self):
        """VID/PID match with unavailable product strings → high."""
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0", vid=0x303A, pid=0x8120, product=None, manufacturer=None
        )
        result = device_detect._match_fingerprint(
            info, device_detect._CARDCOMPUTER_FINGERPRINTS
        )
        assert result == "high"

    def test_vid_pid_match_with_mismatched_manufacturer_medium(self):
        """VID/PID match but wrong manufacturer → medium confidence."""
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0",
            vid=0x303A,
            pid=0x8120,
            product="SomeOtherProduct",
            manufacturer="WrongVendor",
        )
        result = device_detect._match_fingerprint(
            info, device_detect._CARDCOMPUTER_FINGERPRINTS
        )
        assert result == "medium"

    def test_vid_pid_match_with_mismatched_product_medium(self):
        """VID/PID match but wrong product → medium confidence."""
        info = device_detect.DeviceInfo(
            port="/dev/ttyUSB0",
            vid=0x10C4,
            pid=0xEA60,
            product="WrongProduct",
            manufacturer="Silicon Labs",
        )
        result = device_detect._match_fingerprint(
            info, device_detect._RNODE_FINGERPRINTS
        )
        assert result == "medium"


# ---------------------------------------------------------------------------
# _desc_matches_any
# ---------------------------------------------------------------------------


class TestDescMatchesAny:
    """Tests for _desc_matches_any()."""

    def test_matches_keyword_in_description(self):
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0", description="M5Stack Cardputer ADV"
        )
        assert device_detect._desc_matches_any(info, ("cardputer", "m5stack")) is True

    def test_matches_keyword_in_product(self):
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0", product="M5Stack Cardputer", description=""
        )
        assert device_detect._desc_matches_any(info, ("cardputer",)) is True

    def test_matches_keyword_in_manufacturer(self):
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0", manufacturer="M5Stack Technology", description=""
        )
        assert device_detect._desc_matches_any(info, ("m5stack",)) is True

    def test_no_match(self):
        info = device_detect.DeviceInfo(
            port="/dev/ttyACM0", description="FTDI USB Serial"
        )
        assert device_detect._desc_matches_any(info, ("cardputer", "m5stack")) is False


# ---------------------------------------------------------------------------
# detect_devices — both devices present
# ---------------------------------------------------------------------------


class TestDetectDevicesBothPresent:
    """detect_devices() when both Cardputer and RNode are connected."""

    def test_both_detected_with_high_confidence(self):
        ports = [_CARDPUTER_PORT, _RNODE_PORT]
        with _mock_comports(ports):
            result = device_detect.detect_devices()

        assert result.cardputer is not None
        assert result.cardputer.port == "/dev/ttyACM0"
        assert result.rnode is not None
        assert result.rnode.port == "/dev/ttyUSB0"
        assert result.confidence.get("cardputer") == "high"
        assert result.confidence.get("rnode") == "high"
        assert result.cardputer_port == "/dev/ttyACM0"
        assert result.rnode_port == "/dev/ttyUSB0"
        assert len(result.all_ports) == 2


# ---------------------------------------------------------------------------
# detect_devices — only Cardputer present
# ---------------------------------------------------------------------------


class TestDetectDevicesCardputerOnly:
    """detect_devices() when only Cardputer is connected."""

    def test_cardputer_detected_rnode_none(self):
        with _mock_comports([_CARDPUTER_PORT]):
            result = device_detect.detect_devices()

        assert result.cardputer is not None
        assert result.cardputer.port == "/dev/ttyACM0"
        assert result.rnode is None
        assert result.rnode_port is None


# ---------------------------------------------------------------------------
# detect_devices — only RNode present
# ---------------------------------------------------------------------------


class TestDetectDevicesRnodeOnly:
    """detect_devices() when only RNode is connected."""

    def test_rnode_detected_cardputer_none(self):
        with _mock_comports([_RNODE_PORT]):
            result = device_detect.detect_devices()

        assert result.rnode is not None
        assert result.rnode.port == "/dev/ttyUSB0"
        assert result.cardputer is None
        assert result.cardputer_port is None


# ---------------------------------------------------------------------------
# detect_devices — swapped enumeration order
# ---------------------------------------------------------------------------


class TestDetectDevicesSwappedOrder:
    """detect_devices() with ports in reverse order."""

    def test_rnode_first_cardputer_second(self):
        """Detection works regardless of port enumeration order."""
        with _mock_comports([_RNODE_PORT, _CARDPUTER_PORT]):
            result = device_detect.detect_devices()

        assert result.rnode is not None
        assert result.rnode.port == "/dev/ttyUSB0"
        assert result.cardputer is not None
        assert result.cardputer.port == "/dev/ttyACM0"

    def test_cardputer_first_rnode_second(self):
        with _mock_comports([_CARDPUTER_PORT, _RNODE_PORT]):
            result = device_detect.detect_devices()

        assert result.cardputer is not None
        assert result.cardputer.port == "/dev/ttyACM0"
        assert result.rnode is not None
        assert result.rnode.port == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# detect_devices — unknown devices
# ---------------------------------------------------------------------------


class TestDetectDevicesUnknown:
    """detect_devices() with unknown/unrecognized devices."""

    def test_unknown_devices_not_classified(self):
        unknown = _make_port(
            device="/dev/ttyUSB1",
            vid=0x0403,
            pid=0x6001,
            product="FT232R USB UART",
            manufacturer="FTDI",
        )
        with _mock_comports([unknown]):
            result = device_detect.detect_devices()

        assert result.cardputer is None
        assert result.rnode is None
        assert len(result.all_ports) == 1

    def test_no_ports_at_all(self):
        """Empty port list returns clean result with no devices."""
        with _mock_comports([]):
            result = device_detect.detect_devices()

        assert result.cardputer is None
        assert result.rnode is None
        assert result.all_ports == []

    def test_non_usb_ports_not_matched(self):
        """Serial ports without VID/PID are included in all_ports but not classified."""
        non_usb = _make_port(device="/dev/ttyS0", vid=None, pid=None)
        with _mock_comports([non_usb]):
            result = device_detect.detect_devices()

        assert result.cardputer is None
        assert result.rnode is None
        assert len(result.all_ports) == 1
        assert result.all_ports[0].port == "/dev/ttyS0"


# ---------------------------------------------------------------------------
# detect_devices — NO cross-matching
# ---------------------------------------------------------------------------


class TestDetectDevicesNoCrossMatching:
    """Verify that Cardputer is NOT matched as RNode and vice versa."""

    def test_cardputer_not_matched_as_rnode(self):
        """The Cardputer on VID:0x303A PID:0x8120 must NOT be detected as RNode."""
        with _mock_comports([_CARDPUTER_PORT]):
            result = device_detect.detect_devices()

        assert result.cardputer is not None
        assert result.rnode is None  # ← key assertion: no cross-match

    def test_rnode_not_matched_as_cardputer(self):
        """The RNode on VID:0x10C4 PID:0xEA60 (CP2102) must NOT be detected as
        Cardputer — this was the original bug.
        """
        with _mock_comports([_RNODE_PORT]):
            result = device_detect.detect_devices()

        assert result.rnode is not None
        assert result.cardputer is None  # ← key assertion: no cross-match

    def test_cp210x_not_cardputer(self):
        """A generic CP210x device (VID 0x10C4) with the RNode PID must NOT
        match as Cardputer — even though old code matched 'cp210x' keyword.
        """
        cp210x = _make_port(
            device="/dev/ttyUSB0",
            vid=0x10C4,
            pid=0xEA60,
            product="CP2102 USB to UART Bridge Controller",
            manufacturer="Silicon Labs",
        )
        with _mock_comports([cp210x]):
            result = device_detect.detect_devices()

        assert result.cardputer is None, (
            "CP210x device must NOT be classified as Cardputer "
            "(this was the original cross-matching bug)"
        )
        assert result.rnode is not None

    def test_esp32_non_cardputer_not_matched_as_cardputer(self):
        """An ESP32 device (VID 0x303A) with a non-Cardputer PID should NOT
        match as Cardputer unless the strings match.
        """
        other_esp32 = _make_port(
            device="/dev/ttyACM0",
            vid=0x303A,
            pid=0x1001,  # RNode firmware PID
            product="RNode Firmware",
            manufacturer="SomeVendor",
        )
        with _mock_comports([other_esp32]):
            result = device_detect.detect_devices()

        # Should not match Cardputer (wrong PID 0x1001 vs 0x8120)
        assert result.cardputer is None

    def test_ch340_not_cardputer(self):
        """A CH340 (VID 0x1A86) must NOT be matched as Cardputer — old keyword
        matching would falsely match on 'ch340'.
        """
        ch340 = _make_port(
            device="/dev/ttyUSB1",
            vid=0x1A86,
            pid=0x7523,
            product="USB Serial",
            manufacturer="",
        )
        with _mock_comports([ch340]):
            result = device_detect.detect_devices()

        assert result.cardputer is None, (
            "CH340 device must NOT be classified as Cardputer "
            "(keyword fallback was removed)"
        )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


class TestFindCardputerPort:
    """Tests for find_cardputer_port()."""

    def test_preferred_returned_immediately(self):
        """When preferred is given, return it without scanning."""
        with patch("serial.tools.list_ports.comports") as mock_comports:
            result = device_detect.find_cardputer_port(preferred="/dev/ttyS0")
        mock_comports.assert_not_called()
        assert result == "/dev/ttyS0"

    def test_cardputer_detected(self):
        """Cardputer connected → port returned."""
        with _mock_comports([_CARDPUTER_PORT]):
            result = device_detect.find_cardputer_port()
        assert result == "/dev/ttyACM0"

    def test_no_cardputer_returns_none(self):
        """No Cardputer → None."""
        with _mock_comports([_RNODE_PORT]):
            result = device_detect.find_cardputer_port()
        assert result is None


class TestFindRnodePort:
    """Tests for find_rnode_port()."""

    def test_preferred_returned_immediately(self):
        """When preferred is given, return it without scanning."""
        with patch("serial.tools.list_ports.comports") as mock_comports:
            result = device_detect.find_rnode_port(preferred="/dev/ttyUSB2")
        mock_comports.assert_not_called()
        assert result == "/dev/ttyUSB2"

    def test_rnode_detected(self):
        """RNode connected → port returned."""
        with _mock_comports([_RNODE_PORT]):
            result = device_detect.find_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_no_rnode_returns_none(self):
        """No RNode → None."""
        with _mock_comports([_CARDPUTER_PORT]):
            result = device_detect.find_rnode_port()
        assert result is None


# ---------------------------------------------------------------------------
# Protocol probes
# ---------------------------------------------------------------------------


class TestProbeRnode:
    """Tests for probe_rnode()."""

    def test_valid_rnode_response(self):
        """Valid DETECT response → True."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = bytes([0xC0, 0x08, 0x46, 0xC0])

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_rnode("/dev/ttyUSB0")
        assert result is True

    def test_no_response(self):
        """No data → False."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_rnode("/dev/ttyUSB0")
        assert result is False

    def test_unexpected_response(self):
        """Wrong data → False."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b"garbage\x00\x01\x02"

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_rnode("/dev/ttyUSB0")
        assert result is False

    def test_short_response(self):
        """Response shorter than 4 bytes → False."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b"\xC0\x08"

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_rnode("/dev/ttyUSB0")
        assert result is False

    def test_serial_exception(self):
        """Serial exception → False (never hangs)."""
        with patch("serial.Serial", side_effect=OSError("denied")):
            result = device_detect.probe_rnode("/dev/ttyUSB0")
        assert result is False

    def test_sends_correct_detect_command(self):
        """Probe sends the standard RNode DETECT command."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""

        with patch("serial.Serial", return_value=mock_ser):
            device_detect.probe_rnode("/dev/ttyUSB0")

        # Verify the correct DETECT command was sent
        mock_ser.write.assert_called_with(bytes([0xC0, 0x08, 0x73, 0xC0]))


class TestProbeCardputerRepl:
    """Tests for probe_cardputer_repl()."""

    def test_micropython_banner_detected(self):
        """Raw REPL banner in response → True."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0
        mock_ser.read.return_value = b"raw REPL; CTRL-B to exit\r\n>"

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_cardputer_repl("/dev/ttyACM0")
        assert result is True

    def test_micropython_keyword_detected(self):
        """'micropython' in response → True."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0
        mock_ser.read.return_value = b"MicroPython v1.23.0"

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_cardputer_repl("/dev/ttyACM0")
        assert result is True

    def test_no_banner(self):
        """No matching banner → False."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0
        mock_ser.read.return_value = b"garbage"

        with patch("serial.Serial", return_value=mock_ser):
            result = device_detect.probe_cardputer_repl("/dev/ttyACM0")
        assert result is False

    def test_serial_exception(self):
        """Serial exception → False (never hangs)."""
        with patch("serial.Serial", side_effect=OSError("denied")):
            result = device_detect.probe_cardputer_repl("/dev/ttyACM0")
        assert result is False

    def test_sends_raw_repl_sequence(self):
        """Probe sends Ctrl+C×2 + Ctrl+A to enter raw REPL."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 0
        mock_ser.read.return_value = b""

        with patch("serial.Serial", return_value=mock_ser):
            device_detect.probe_cardputer_repl("/dev/ttyACM0")

        # Should send \r\x03\x03 (Ctrl+C×2) then \r\x01 (Ctrl+A)
        write_calls = [c[0][0] for c in mock_ser.write.call_args_list]
        assert b"\r\x03\x03" in write_calls
        assert b"\r\x01" in write_calls


# ---------------------------------------------------------------------------
# detect_devices — pyserial unavailable
# ---------------------------------------------------------------------------


class TestDetectDevicesNoPyserial:
    """detect_devices() when pyserial is not importable."""

    def test_import_error_returns_empty(self):
        """When serial.tools.list_ports raises ImportError, return empty result."""
        with patch(
            "serial.tools.list_ports.comports",
            side_effect=ImportError("no pyserial"),
        ):
            result = device_detect.detect_devices()

        assert result.cardputer is None
        assert result.rnode is None
        assert result.all_ports == []

    def test_comports_exception_returns_empty(self):
        """When comports() raises an exception, return empty result."""
        with patch(
            "serial.tools.list_ports.comports",
            side_effect=OSError("permission denied"),
        ):
            result = device_detect.detect_devices()

        assert result.cardputer is None
        assert result.rnode is None
        assert result.all_ports == []


# ---------------------------------------------------------------------------
# DetectionResult dataclass
# ---------------------------------------------------------------------------


class TestDetectionResult:
    """Dedicated tests for DetectionResult dataclass."""

    def test_defaults(self):
        r = device_detect.DetectionResult()
        assert r.cardputer is None
        assert r.rnode is None
        assert r.confidence == {}
        assert r.all_ports == []
        assert r.cardputer_port is None
        assert r.rnode_port is None

    def test_cardputer_port_property(self):
        info = device_detect.DeviceInfo(port="/dev/ttyACM0")
        r = device_detect.DetectionResult(cardputer=info)
        assert r.cardputer_port == "/dev/ttyACM0"

    def test_rnode_port_property(self):
        info = device_detect.DeviceInfo(port="/dev/ttyUSB0")
        r = device_detect.DetectionResult(rnode=info)
        assert r.rnode_port == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# Multiple devices of same type
# ---------------------------------------------------------------------------


class TestDetectDevicesMultipleSameType:
    """When multiple devices match the same fingerprint, pick the first one."""

    def test_two_cardputers(self):
        cp1 = _make_port(
            device="/dev/ttyACM0",
            vid=0x303A,
            pid=0x8120,
            product="M5Stack UiFlow 2.0",
            manufacturer="M5Stack Technology Co., Ltd",
        )
        cp2 = _make_port(
            device="/dev/ttyACM1",
            vid=0x303A,
            pid=0x8120,
            product="M5Stack UiFlow 2.0",
            manufacturer="M5Stack Technology Co., Ltd",
        )
        with _mock_comports([cp1, cp2]):
            result = device_detect.detect_devices()

        assert result.cardputer is not None
        assert result.cardputer.port == "/dev/ttyACM0"  # first one wins

    def test_two_rnodes(self):
        rn1 = _make_port(
            device="/dev/ttyUSB0",
            vid=0x10C4,
            pid=0xEA60,
            product="CP2102 USB to UART Bridge Controller",
            manufacturer="Silicon Labs",
        )
        rn2 = _make_port(
            device="/dev/ttyUSB1",
            vid=0x10C4,
            pid=0xEA60,
            product="CP2102 USB to UART Bridge Controller",
            manufacturer="Silicon Labs",
        )
        with _mock_comports([rn1, rn2]):
            result = device_detect.detect_devices()

        assert result.rnode is not None
        assert result.rnode.port == "/dev/ttyUSB0"  # first one wins


if __name__ == "__main__":
    import sys as _sys

    import pytest as _pytest

    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
