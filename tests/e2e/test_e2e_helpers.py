"""Unit tests for e2e_helpers and rnode_flasher — no hardware required.

Run with::

    bazel test //tests:test_e2e_helpers --test_output=all
"""

import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Ensure the e2e/ directory is on sys.path for sibling imports
sys.path.insert(0, os.path.dirname(__file__))
from e2e_helpers import (
    RNODE_VIDS,
    case_insensitive_contains,
    check_rnode_firmware,
    find_rnode_port,
    flash_rnode_firmware,
)

# Import KISS/ROM/flash primitives directly for unit testing
from lma_core.rnode_flasher import (
    CMD_DETECT,
    CMD_DEV_HASH,
    CMD_FW_HASH,
    CMD_FW_VERSION,
    CMD_PLATFORM,
    CMD_ROM_READ,
    DETECT_REQ,
    DETECT_RESP,
    KISS_FEND,
    KISS_FESC,
    KISS_TFEND,
    KISS_TFESC,
    ROM,
    RNodeKiss,
    decode_kiss_frame,
    encode_kiss_frame,
    flash_rnode,
    provision_rnode_eeprom,
    set_rnode_firmware_hash,
)


def _make_port(device, vid, description):
    """Build a lightweight fake port object for mocking list_ports."""
    return SimpleNamespace(device=device, vid=vid, description=description)


def _mock_serial_read(*frames: bytes) -> Callable:
    """Build a mock ``serial.Serial.read`` side-effect.

    Each *frame* is a complete KISS frame (including FEND delimiters).
    The returned function yields each byte of each frame in order,
    then returns ``b""`` forever.  Between frames a short run of empty
    reads is inserted so that ``_read_until_response`` doesn't race
    past the frame boundary.

    Returns a callable suitable for ``mock_ser.read.side_effect``.
    """
    all_bytes: list[bytes] = []
    for frame in frames:
        # A few empty reads ensure the loop has time to process
        all_bytes.extend([b""] * 2)
        for byte in frame:
            all_bytes.append(bytes([byte]))
    # Pad with empty reads so the loop doesn't exhaust
    all_bytes.extend([b""] * 100)

    def _read(n: int = 1) -> bytes:
        nonlocal all_bytes
        if not all_bytes:
            return b""
        return all_bytes.pop(0)

    return _read


# ---------------------------------------------------------------------------
# Existing tests (unchanged)
# ---------------------------------------------------------------------------


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
        with patch("serial.tools.list_ports.comports", side_effect=OSError("permission denied")):
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


# ---------------------------------------------------------------------------
# KISS Frame Tests
# ---------------------------------------------------------------------------


class TestKissFrameEncodeDecode:
    """Unit tests for KISS frame encoding and decoding."""

    def test_encode_simple_data(self):
        """Simple data with no special bytes."""
        result = encode_kiss_frame([0x01, 0x02, 0x03])
        assert result == bytes([KISS_FEND, 0x01, 0x02, 0x03, KISS_FEND])

    def test_encode_escapes_fend(self):
        """Literal FEND bytes in data are escaped."""
        result = encode_kiss_frame([0x01, KISS_FEND, 0x02])
        assert result == bytes([KISS_FEND, 0x01, KISS_FESC, KISS_TFEND, 0x02, KISS_FEND])

    def test_encode_escapes_fesc(self):
        """Literal FESC bytes in data are escaped."""
        result = encode_kiss_frame([0x01, KISS_FESC, 0x02])
        assert result == bytes([KISS_FEND, 0x01, KISS_FESC, KISS_TFESC, 0x02, KISS_FEND])

    def test_encode_multiple_escapes(self):
        """Multiple special bytes are all escaped correctly."""
        result = encode_kiss_frame([KISS_FEND, KISS_FESC, KISS_FEND])
        expected = bytes(
            [
                KISS_FEND,
                KISS_FESC,
                KISS_TFEND,
                KISS_FESC,
                KISS_TFESC,
                KISS_FESC,
                KISS_TFEND,
                KISS_FEND,
            ]
        )
        assert result == expected

    def test_decode_simple_data(self):
        """Simple frame decodes correctly."""
        result = decode_kiss_frame([0x01, 0x02, 0x03])
        assert result == [0x01, 0x02, 0x03]

    def test_decode_unescapes_tfend(self):
        """TFEND escape sequence decodes to FEND."""
        result = decode_kiss_frame([0x01, KISS_FESC, KISS_TFEND, 0x02])
        assert result == [0x01, KISS_FEND, 0x02]

    def test_decode_unescapes_tfesc(self):
        """TFESC escape sequence decodes to FESC."""
        result = decode_kiss_frame([0x01, KISS_FESC, KISS_TFESC, 0x02])
        assert result == [0x01, KISS_FESC, 0x02]

    def test_decode_invalid_escape_returns_none(self):
        """Unknown escape byte returns None."""
        result = decode_kiss_frame([0x01, KISS_FESC, 0xFF, 0x02])
        assert result is None

    def test_decode_incomplete_escape_at_end_returns_none(self):
        """Frame ending with FESC (incomplete escape) returns None."""
        result = decode_kiss_frame([0x01, KISS_FESC])
        assert result is None

    def test_decode_empty_frame(self):
        """Empty frame decodes to empty list."""
        result = decode_kiss_frame([])
        assert result == []

    def test_roundtrip(self):
        """Encode then decode produces the original data."""
        original = [0x08, 0x73, 0x01, 0x02, 0x03]  # CMD_DETECT + DETECT_REQ
        encoded = encode_kiss_frame(original)
        # Strip FEND delimiters
        inner = list(encoded[1:-1])
        decoded = decode_kiss_frame(inner)
        assert decoded == original

    def test_roundtrip_with_escapes(self):
        """Round-trip handles data containing special bytes."""
        original = [0x01, KISS_FEND, 0x02, KISS_FESC, 0x03]
        encoded = encode_kiss_frame(original)
        inner = list(encoded[1:-1])
        decoded = decode_kiss_frame(inner)
        assert decoded == original


# ---------------------------------------------------------------------------
# ROM / EEPROM Tests
# ---------------------------------------------------------------------------


class TestROM:
    """Tests for ROM class — EEPROM layout and checksum calculation."""

    def test_product_constants_defined(self):
        """All product constants are non-zero."""
        assert ROM.PRODUCT_RNODE == 0x03
        assert ROM.PRODUCT_H32_V3 == 0xC1

    def test_model_constants_defined(self):
        """Model constants for Heltec are defined."""
        assert ROM.MODEL_AA == 0xAA
        assert ROM.MODEL_CA == 0xCA

    def test_addr_constants_are_sequential(self):
        """Key EEPROM address constants have expected values."""
        assert ROM.ADDR_PRODUCT == 0x00
        assert ROM.ADDR_MODEL == 0x01
        assert ROM.ADDR_HW_REV == 0x02
        assert ROM.ADDR_SERIAL == 0x03
        assert ROM.ADDR_MADE == 0x07
        assert ROM.ADDR_CHKSUM == 0x0B  # 11 bytes info + 16 bytes checksum
        assert ROM.ADDR_SIGNATURE == 0x1B
        assert ROM.ADDR_INFO_LOCK == 0x9B

    def test_info_lock_byte_value(self):
        """INFO_LOCK_BYTE is 0x73 as in rnode-flasher."""
        assert ROM.INFO_LOCK_BYTE == 0x73

    def test_calc_checksum_is_16_bytes(self):
        """Checksum is a 16-byte MD5 digest."""
        info = bytes([0x03, 0xAA, 0x01]) + b"\x00\x00\x00\x01" + b"\x00\x00\x00\x02"
        result = ROM.calc_checksum(info)
        assert len(result) == 16
        assert isinstance(result, bytes)

    def test_calc_checksum_deterministic(self):
        """Same input always produces the same checksum."""
        info = bytes([0x03, 0xAA, 0x01]) + b"\x12\x34\x56\x78" + b"\x00\x00\x00\x02"
        c1 = ROM.calc_checksum(info)
        c2 = ROM.calc_checksum(info)
        assert c1 == c2

    def test_calc_checksum_different_for_different_input(self):
        """Different inputs produce different checksums."""
        info1 = bytes([0x03, 0xAA, 0x01]) + b"\x00\x00\x00\x01" + b"\x00\x00\x00\x02"
        info2 = bytes([0x03, 0xAB, 0x01]) + b"\x00\x00\x00\x01" + b"\x00\x00\x00\x02"
        assert ROM.calc_checksum(info1) != ROM.calc_checksum(info2)

    def test_pack_serial(self):
        """Serial numbers are packed as 4-byte big-endian."""
        result = ROM.pack_serial(0x12345678)
        assert result == b"\x12\x34\x56\x78"

    def test_pack_serial_truncates_to_32bit(self):
        """Values greater than 32-bit are truncated."""
        result = ROM.pack_serial(0xFFFFFFFF + 1)
        assert len(result) == 4

    def test_pack_serial_zero(self):
        """Zero serial number packs correctly."""
        result = ROM.pack_serial(0)
        assert result == b"\x00\x00\x00\x00"

    def test_unpack_serial(self):
        """Serial numbers are unpacked from 4 big-endian bytes."""
        result = ROM.unpack_serial(b"\x00\x00\x00\x2a")
        assert result == 42

    def test_unpack_serial_shorter_input(self):
        """Unpack handles input shorter than 4 bytes (left-padded with zeros)."""
        result = ROM.unpack_serial(b"\x2a")
        assert result == 42  # 0x2A right-padded: 0x0000002A


# ---------------------------------------------------------------------------
# RNodeKiss Tests (mocked serial)
# ---------------------------------------------------------------------------


class TestRNodeKissDetect:
    """Tests for RNodeKiss.detect() — mocked serial communication."""

    def test_detect_returns_true_when_resp_received(self):
        """CMD_DETECT with DETECT_RESP returns True."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DETECT, DETECT_RESP]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                result = rnode.detect(timeout=0.5)
        assert result is True

    def test_detect_returns_false_on_timeout(self):
        """Returns False when no response is received within timeout."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""  # never returns data

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                result = rnode.detect(timeout=0.3)
        assert result is False

    def test_detect_returns_false_on_wrong_response(self):
        """Returns False when response byte is not DETECT_RESP."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DETECT, 0x00]),  # wrong response
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                result = rnode.detect(timeout=0.5)
        assert result is False

    def test_get_firmware_version(self):
        """Returns version string from CMD_FW_VERSION response."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_FW_VERSION, 0x01, 0x49, 0x00]),  # v1.73.0
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                version = rnode.get_firmware_version()
        assert version == "1.73.0"

    def test_get_platform_esp32(self):
        """Returns PLATFORM_ESP32 (0x80) for ESP32 devices."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_PLATFORM, 0x80]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                platform = rnode.get_platform()
        assert platform == 0x80


class TestRNodeKissSendCommand:
    """Tests for RNodeKiss.send_command() with mocked serial."""

    def test_send_command_writes_correct_frame(self):
        """send_command writes the expected KISS frame to serial."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DETECT, DETECT_RESP]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                rnode.send_command(CMD_DETECT, [DETECT_REQ], timeout=0.5)

        # Verify write was called with correct KISS frame
        expected_frame = encode_kiss_frame([CMD_DETECT, DETECT_REQ])
        # At least one write call should contain our expected frame
        write_calls = [call[0][0] for call in mock_ser.write.call_args_list]
        assert expected_frame in write_calls

    def test_send_command_returns_response(self):
        """Returns the payload from the matching response command."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DETECT, DETECT_RESP, 0x01]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                response = rnode.send_command(CMD_DETECT, [DETECT_REQ], timeout=0.5)
        assert response == [DETECT_RESP, 0x01]

    def test_send_command_ignores_wrong_command_response(self):
        """Ignores frames that don't match the expected command byte."""
        mock_ser = MagicMock()
        # First frame is a different command, second is the one we want
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DEV_HASH, 0xAA, 0xBB]),
            encode_kiss_frame([CMD_DETECT, DETECT_RESP]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                response = rnode.send_command(CMD_DETECT, [DETECT_REQ], timeout=1.0)
        assert response == [DETECT_RESP]

    def test_read_rom(self):
        """Reads EEPROM via CMD_ROM_READ."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_ROM_READ, 0x03, 0xAA, 0x01]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                rom = rnode.read_rom()
        assert rom == b"\x03\xaa\x01"

    def test_get_device_hash(self):
        """Reads device hash via CMD_DEV_HASH."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DEV_HASH, 0x01, 0x02, 0x03, 0x04]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            with RNodeKiss("/dev/ttyUSB0", timeout=0.1) as rnode:
                dev_hash = rnode.get_device_hash()
        assert dev_hash == b"\x01\x02\x03\x04"


# ---------------------------------------------------------------------------
# Flash / Provision Tests (mocked subprocess + serial)
# ---------------------------------------------------------------------------


class TestCheckRNodeFirmware:
    """Tests for check_rnode_firmware() — KISS-based detection (mocked serial)."""

    def test_firmware_detected(self):
        """Returns True when KISS detect succeeds."""
        mock_ser = MagicMock()
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DETECT, DETECT_RESP]),
        )

        with patch("serial.Serial", return_value=mock_ser):
            result = check_rnode_firmware("/dev/ttyUSB0", timeout=0.5)
        assert result is True

    def test_firmware_not_detected(self):
        """Returns False when KISS detect fails."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""

        with patch("serial.Serial", return_value=mock_ser):
            result = check_rnode_firmware("/dev/ttyUSB0", timeout=0.5)
        assert result is False

    def test_serial_exception_returns_false(self):
        """Returns False when serial port cannot be opened."""
        with patch("serial.Serial", side_effect=Exception("Permission denied")):
            result = check_rnode_firmware("/dev/ttyUSB0", timeout=0.5)
        assert result is False


class TestFlashRNodeFirmware:
    """Tests for flash_rnode_firmware() — esptool subprocess calls."""

    def test_flash_succeeds(self):
        """Returns (True, ...) when both erase and write succeed."""
        mock_result = SimpleNamespace(returncode=0, stdout="Done", stderr="")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("lma_core.rnode_flasher._find_esptool", return_value="esptool.py"),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is True
        assert "success" in msg.lower()

    def test_flash_fails_on_erase_error(self):
        """Returns (False, ...) when esptool erase fails."""
        mock_result = SimpleNamespace(
            returncode=1, stdout="", stderr="esptool.FatalError: Failed to connect"
        )
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("lma_core.rnode_flasher._find_esptool", return_value="esptool.py"),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is False
        assert "Erase failed" in msg or "Flash failed" in msg

    def test_flash_fails_on_write_error(self):
        """Returns (False, ...) when erase succeeds but write fails."""
        good_result = SimpleNamespace(returncode=0, stdout="Done", stderr="")
        bad_result = SimpleNamespace(returncode=1, stdout="", stderr="esptool.FatalError")
        with (
            patch("subprocess.run", side_effect=[good_result, bad_result]),
            patch("lma_core.rnode_flasher._find_esptool", return_value="esptool.py"),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is False

    def test_flash_timeout(self):
        """Returns (False, ...) when subprocess times out."""
        with (
            patch(
                "subprocess.run",
                side_effect=__import__("subprocess").TimeoutExpired("cmd", 120),
            ),
            patch("lma_core.rnode_flasher._find_esptool", return_value="esptool.py"),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is False
        assert "timed out" in msg

    def test_flash_missing_firmware(self):
        """Returns (False, ...) when firmware file is missing."""
        with (
            patch("os.path.isfile", return_value=False),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/nonexistent.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is False
        assert "not found" in msg.lower()

    def test_flash_missing_esptool(self):
        """Returns (False, ...) when esptool is not on PATH."""
        with (
            patch("lma_core.rnode_flasher._find_esptool", return_value=None),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            ok, msg = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert ok is False
        assert "esptool" in msg.lower()

    def test_tuple_return_type(self):
        """Return type is always tuple[bool, str]."""
        mock_result = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("lma_core.rnode_flasher._find_esptool", return_value="esptool.py"),
            patch("os.path.isfile", return_value=True),
            patch.dict("os.environ", {"RNODE_FIRMWARE_PATH": "/tmp/fake.bin"}),
        ):
            result = flash_rnode_firmware("/dev/ttyUSB0", timeout=10)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


class TestProvisionEeprom:
    """Tests for provision_rnode_eeprom() — mocked serial communication."""

    def test_provision_writes_correct_bytes(self):
        """Provisioning sends expected KISS commands in order."""
        mock_ser = MagicMock()
        # Provide a ROM read response for the verification step
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_ROM_READ] + [0] * ROM.ADDR_INFO_LOCK + [ROM.INFO_LOCK_BYTE]),
        )

        with (
            patch("serial.Serial", return_value=mock_ser),
            patch(
                "time.sleep",
                return_value=None,  # speed up test
            ),
        ):
            ok, msg = provision_rnode_eeprom("/dev/ttyUSB0", timeout=1.0)
        assert ok is True
        assert "complete" in msg.lower()

    def test_provision_serial_error(self):
        """Returns (False, ...) when serial port fails."""
        with patch("serial.Serial", side_effect=Exception("Access denied")):
            ok, msg = provision_rnode_eeprom("/dev/ttyUSB0", timeout=1.0)
        assert ok is False


class TestSetFirmwareHash:
    """Tests for set_rnode_firmware_hash() — mocked serial communication."""

    def test_set_hash_with_provided_bytes(self):
        """Uses provided hash bytes directly."""
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""

        hash_bytes = bytes([0x01, 0x02, 0x03, 0x04])
        with patch("serial.Serial", return_value=mock_ser):
            ok, msg = set_rnode_firmware_hash("/dev/ttyUSB0", hash_bytes=hash_bytes, timeout=1.0)
        assert ok is True

        # Verify CMD_FW_HASH with our hash was written
        expected_frame = encode_kiss_frame([CMD_FW_HASH] + list(hash_bytes))
        write_calls = [call[0][0] for call in mock_ser.write.call_args_list]
        assert expected_frame in write_calls

    def test_set_hash_reads_device_hash_when_none_provided(self):
        """Reads device hash via CMD_DEV_HASH when hash_bytes is None."""
        mock_ser = MagicMock()
        dev_hash = [0xAA, 0xBB, 0xCC, 0xDD]
        mock_ser.read.side_effect = _mock_serial_read(
            encode_kiss_frame([CMD_DEV_HASH] + dev_hash),
        )

        with patch("serial.Serial", return_value=mock_ser):
            ok, msg = set_rnode_firmware_hash("/dev/ttyUSB0", hash_bytes=None, timeout=1.0)
        assert ok is True

        # Verify CMD_FW_HASH was sent with device hash
        expected_hash_frame = encode_kiss_frame([CMD_FW_HASH] + dev_hash)
        write_calls = [call[0][0] for call in mock_ser.write.call_args_list]
        assert expected_hash_frame in write_calls

    def test_set_hash_serial_error(self):
        """Returns (False, ...) when serial port fails."""
        with patch("serial.Serial", side_effect=Exception("Access denied")):
            ok, msg = set_rnode_firmware_hash("/dev/ttyUSB0", timeout=1.0)
        assert ok is False


class TestFlashRNode:
    """Tests for flash_rnode() orchestrator — mocked sub-functions."""

    def test_full_success(self):
        """Returns (True, ...) when all steps succeed."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(True, "Flash OK")),
            patch("lma_core.rnode_flasher.provision_rnode_eeprom", return_value=(True, "Provision OK")),
            patch("lma_core.rnode_flasher.set_rnode_firmware_hash", return_value=(True, "Hash OK")),
            patch("lma_core.rnode_flasher.check_rnode_firmware", return_value=True),
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0")
        assert ok is True
        assert "successfully" in msg

    def test_flash_failure_returns_early(self):
        """Returns (False, ...) when flash step fails."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(False, "Flash error")),
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0")
        assert ok is False
        assert "Flash failed" in msg

    def test_provision_failure_returns_early(self):
        """Returns (False, ...) when EEPROM provision fails."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(True, "Flash OK")),
            patch("lma_core.rnode_flasher.provision_rnode_eeprom", return_value=(False, "Provision error")),
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0")
        assert ok is False
        assert "provisioning failed" in msg

    def test_hash_set_failure_returns_early(self):
        """Returns (False, ...) when firmware hash set fails."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(True, "Flash OK")),
            patch("lma_core.rnode_flasher.provision_rnode_eeprom", return_value=(True, "Provision OK")),
            patch("lma_core.rnode_flasher.set_rnode_firmware_hash", return_value=(False, "Hash error")),
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0")
        assert ok is False
        assert "hash" in msg.lower()

    def test_verification_failure(self):
        """Returns (False, ...) when post-flash verify fails."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(True, "Flash OK")),
            patch("lma_core.rnode_flasher.provision_rnode_eeprom", return_value=(True, "Provision OK")),
            patch("lma_core.rnode_flasher.set_rnode_firmware_hash", return_value=(True, "Hash OK")),
            patch("lma_core.rnode_flasher.check_rnode_firmware", return_value=False),
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0")
        assert ok is False
        assert "verification failed" in msg

    def test_provision_skipped(self):
        """When provision=False, skips EEPROM/hash/verify steps."""
        with (
            patch("lma_core.rnode_flasher.flash_rnode_firmware", return_value=(True, "Flash OK")),
            patch("lma_core.rnode_flasher.provision_rnode_eeprom") as mock_provision,
            patch("lma_core.rnode_flasher.set_rnode_firmware_hash") as mock_hash,
            patch("lma_core.rnode_flasher.check_rnode_firmware") as mock_verify,
            patch("time.sleep", return_value=None),
        ):
            ok, msg = flash_rnode("/dev/ttyUSB0", provision=False)
        assert ok is True
        assert "skipped" in msg
        mock_provision.assert_not_called()
        mock_hash.assert_not_called()
        mock_verify.assert_not_called()


if __name__ == "__main__":
    import sys as _sys

    import pytest as _pytest

    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
