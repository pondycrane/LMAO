"""RNode firmware flashing via esptool + KISS EEPROM provisioning.

Replaces the fragile ``rnodeconf --autoinstall`` pipeline with a robust,
step-by-step method modelled on the rnode-flasher approach:

1. Flash firmware via ``esptool.py`` subprocess
2. Open KISS serial connection to the freshly-flashed RNode
3. Provision EEPROM via KISS commands (unlock, write info/checksum/signature)
4. Set firmware hash via ``CMD_FW_HASH``
5. Verify each step independently

Usage::

    from lma_core.rnode_flasher import flash_rnode

    ok, msg = flash_rnode("/dev/ttyUSB0")
    assert ok, msg
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import time
import traceback
from collections.abc import Callable

try:
    import serial
    _SerialException = serial.SerialException
except ImportError:
    serial = None  # type: ignore[assignment]
    _SerialException = type("_SerialException", (Exception,), {})

# ---------------------------------------------------------------------------
# KISS Protocol Constants
# ---------------------------------------------------------------------------

KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD

# ---------------------------------------------------------------------------
# RNode Command Constants
# ---------------------------------------------------------------------------

CMD_FREQUENCY = 0x01
CMD_BANDWIDTH = 0x02
CMD_TXPOWER = 0x03
CMD_SF = 0x04
CMD_CR = 0x05
CMD_RADIO_STATE = 0x06

CMD_DETECT = 0x08
DETECT_REQ = 0x73
DETECT_RESP = 0x46

CMD_BOARD = 0x47
CMD_PLATFORM = 0x48
CMD_MCU = 0x49
CMD_FW_VERSION = 0x50
CMD_ROM_READ = 0x51
CMD_ROM_WRITE = 0x52
CMD_CONF_SAVE = 0x53
CMD_CONF_DELETE = 0x54
CMD_RESET = 0x55
CMD_RESET_BYTE = 0xF8
CMD_DEV_HASH = 0x56
CMD_FW_HASH = 0x58
CMD_UNLOCK_ROM = 0x59
ROM_UNLOCK_BYTE = 0xF8
CMD_HASHES = 0x60
CMD_FW_UPD = 0x61
# NOTE: 0x46 is shared with DETECT_RESP above — this is an upstream collision
# in the RNode protocol.  The context (command vs. response) disambiguates.
CMD_BT_CTRL = 0x46

CMD_ERROR = 0x90

# ---------------------------------------------------------------------------
# Platform / MCU Constants
# ---------------------------------------------------------------------------

PLATFORM_AVR = 0x90
PLATFORM_ESP32 = 0x80
PLATFORM_NRF52 = 0x70

MCU_1284P = 0x91
MCU_2560 = 0x92
MCU_ESP32 = 0x81
MCU_NRF52 = 0x71

# ---------------------------------------------------------------------------
# ROM / EEPROM Layout
# ---------------------------------------------------------------------------


class ROM:
    """EEPROM layout constants and checksum calculation.

    Mirrors the ``ROM`` class from rnode-flasher's ``js/rnode.js``.
    """

    # Product codes
    PRODUCT_RNODE = 0x03
    PRODUCT_HMBRW = 0xF0
    PRODUCT_TBEAM = 0xE0
    PRODUCT_TDECK = 0xD0
    PRODUCT_TECHO = 0x15
    PRODUCT_RAK4631 = 0x10
    PRODUCT_T32_10 = 0xB2
    PRODUCT_T32_20 = 0xB0
    PRODUCT_T32_21 = 0xB1
    PRODUCT_H32_V2 = 0xC0
    PRODUCT_H32_V3 = 0xC1
    PRODUCT_H32_V4 = 0xC3
    PRODUCT_HELTEC_T114 = 0xC2
    PRODUCT_TBEAM_S_V1 = 0xEA

    # Model codes
    MODEL_A1 = 0xA1
    MODEL_A2 = 0xA2
    MODEL_A3 = 0xA3
    MODEL_A4 = 0xA4
    MODEL_A5 = 0xA5
    MODEL_A6 = 0xA6
    MODEL_A7 = 0xA7
    MODEL_A8 = 0xA8
    MODEL_A9 = 0xA9
    MODEL_AA = 0xAA
    MODEL_AC = 0xAC
    MODEL_11 = 0x11
    MODEL_12 = 0x12
    MODEL_16 = 0x16
    MODEL_17 = 0x17
    MODEL_FF = 0xFF
    MODEL_FE = 0xFE
    MODEL_BA = 0xBA
    MODEL_BB = 0xBB
    MODEL_B3 = 0xB3
    MODEL_B4 = 0xB4
    MODEL_B8 = 0xB8
    MODEL_B9 = 0xB9
    MODEL_C4 = 0xC4
    MODEL_C5 = 0xC5
    MODEL_C6 = 0xC6
    MODEL_C7 = 0xC7
    MODEL_C8 = 0xC8
    MODEL_C9 = 0xC9
    MODEL_CA = 0xCA
    MODEL_D4 = 0xD4
    MODEL_D9 = 0xD9
    MODEL_DB = 0xDB
    MODEL_DC = 0xDC
    MODEL_E3 = 0xE3
    MODEL_E4 = 0xE4
    MODEL_E8 = 0xE8
    MODEL_E9 = 0xE9

    # EEPROM addresses
    ADDR_PRODUCT = 0x00
    ADDR_MODEL = 0x01
    ADDR_HW_REV = 0x02
    ADDR_SERIAL = 0x03
    ADDR_MADE = 0x07
    ADDR_CHKSUM = 0x0B
    ADDR_SIGNATURE = 0x1B
    ADDR_INFO_LOCK = 0x9B
    ADDR_CONF_SF = 0x9C
    ADDR_CONF_CR = 0x9D
    ADDR_CONF_TXP = 0x9E
    ADDR_CONF_BW = 0x9F
    ADDR_CONF_FREQ = 0xA3
    ADDR_CONF_OK = 0xA7

    INFO_LOCK_BYTE = 0x73
    CONF_OK_BYTE = 0x73

    @staticmethod
    def calc_checksum(data: bytes) -> bytes:
        """Calculate MD5 checksum of EEPROM info fields.

        Equivalent to ``ROM.getCalculatedChecksum()`` in rnode-flasher.
        The checksum is computed over: product, model, hw_rev, serial
        (4 bytes), made (4 bytes) — 11 bytes total.

        Args:
            data: 11-byte slice of EEPROM containing product through made.

        Returns:
            16-byte MD5 digest.
        """
        return hashlib.md5(data).digest()

    @staticmethod
    def pack_serial(serial_num: int) -> bytes:
        """Pack a 32-bit unsigned serial number into 4 big-endian bytes."""
        return struct.pack(">I", serial_num & 0xFFFFFFFF)

    @staticmethod
    def unpack_serial(data: bytes) -> int:
        """Unpack a 4-byte big-endian serial number.

        Pads with leading zero bytes if *data* is shorter than 4 bytes.
        """
        padded = bytes(data[:4]).rjust(4, b"\x00")
        return struct.unpack(">I", padded)[0]


# ---------------------------------------------------------------------------
# KISS Frame Encoding / Decoding
# ---------------------------------------------------------------------------


def decode_kiss_frame(frame: list[int]) -> list[int] | None:
    """Decode a KISS-escaped frame (everything between FEND delimiters).

    Args:
        frame: Raw byte values between two ``KISS_FEND`` bytes.

    Returns:
        Decoded byte values, or ``None`` if an invalid escape sequence
        is encountered.
    """
    data: list[int] = []
    escaping = False
    for byte in frame:
        if escaping:
            if byte == KISS_TFEND:
                data.append(KISS_FEND)
            elif byte == KISS_TFESC:
                data.append(KISS_FESC)
            else:
                return None
            escaping = False
        elif byte == KISS_FESC:
            escaping = True
        else:
            data.append(byte)
    return None if escaping else data


def encode_kiss_frame(data: list[int]) -> bytes:
    """Encode raw bytes into a KISS-delimited frame.

    Args:
        data: Byte values to encode.

    Returns:
        KISS frame bytes (starts and ends with ``KISS_FEND``).
    """
    frame: list[int] = [KISS_FEND]
    for byte in data:
        if byte == KISS_FEND:
            frame.extend((KISS_FESC, KISS_TFEND))
        elif byte == KISS_FESC:
            frame.extend((KISS_FESC, KISS_TFESC))
        else:
            frame.append(byte)
    frame.append(KISS_FEND)
    return bytes(frame)


# ---------------------------------------------------------------------------
# RNodeKiss — Serial KISS transport
# ---------------------------------------------------------------------------


class RNodeKiss:
    """KISS-protocol transport over a serial port.

    Manages frame encoding/decoding, response callbacks keyed by command
    byte, and high-level command methods.

    Intended to be used as a context manager::

        with RNodeKiss("/dev/ttyUSB0") as rnode:
            version = rnode.get_firmware_version()
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 3.0):
        if serial is None:
            raise ImportError("pyserial is required. Install with: pip install pyserial")
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser: serial.Serial | None = None
        self._callbacks: dict[int, Callable] = {}
        self._read_buf: bytearray = bytearray()
        self._in_frame: bool = False
        self._frame_buf: list[int] = []

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> RNodeKiss:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def open(self) -> None:
        """Open the serial port."""
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            timeout=self._timeout,
        )

    def close(self) -> None:
        """Close the serial port."""
        if self._ser is not None and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def is_open(self) -> bool:
        """Return True if the serial port is open."""
        return self._ser is not None and self._ser.is_open

    # -- low-level I/O -----------------------------------------------------

    def _write(self, data: bytes) -> None:
        """Write raw bytes to the serial port."""
        if self._ser is None:
            raise RuntimeError("Serial port not open")
        self._ser.write(data)

    def _read_byte(self) -> int | None:
        """Read a single byte from the serial port (blocking read with timeout)."""
        if self._ser is None:
            return None
        b = self._ser.read(1)
        if not b:
            return None
        return b[0]

    def _read_until_response(self, command: int, timeout: float = 3.0) -> list[int] | None:
        """Read serial input until a response for *command* is received.

        Blocks for up to *timeout* seconds.  KISS frames are decoded and
        dispatched to registered callbacks.

        Returns:
            The response bytes (excluding the command byte), or ``None``
            on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            byte = self._read_byte()
            if byte is None:
                time.sleep(0.01)
                continue
            if byte == KISS_FEND:
                if self._in_frame:
                    decoded = decode_kiss_frame(self._frame_buf)
                    self._frame_buf = []
                    if decoded and len(decoded) > 0:
                        cmd = decoded[0]
                        payload = decoded[1:]
                        cb = self._callbacks.get(cmd)
                        if cb is not None:
                            cb(payload)
                        if cmd == command:
                            return payload
                    elif decoded is None:
                        print(
                            f"WARNING: Invalid KISS frame discarded "
                            f"({len(self._frame_buf)} bytes)",
                            file=sys.stderr,
                        )
                self._in_frame = not self._in_frame
            elif self._in_frame:
                self._frame_buf.append(byte)
        return None

    def send_command(self, command: int, data: list[int], timeout: float = 3.0) -> list[int] | None:
        """Send a KISS command and wait for the response.

        Args:
            command: Command byte.
            data: Payload bytes (after the command byte).
            timeout: Seconds to wait for a response.

        Returns:
            Response payload (excluding command byte), or ``None`` on timeout.
        """
        # Register a temporary callback for this command
        response_container: list[list[int]] = []

        def _cb(payload: list[int]) -> None:
            response_container.append(payload)

        self._callbacks[command] = _cb
        try:
            frame = encode_kiss_frame([command] + data)
            self._write(frame)
        except Exception:
            self._callbacks.pop(command, None)
            raise

        # Wait for response
        result = self._read_until_response(command, timeout=timeout)
        self._callbacks.pop(command, None)

        # If we already captured via callback, prefer that
        if response_container:
            return response_container[0]
        return result

    def send_kiss_command(self, data: list[int]) -> None:
        """Send a KISS frame without waiting for a response."""
        frame = encode_kiss_frame(data)
        self._write(frame)

    # -- high-level commands -----------------------------------------------

    def detect(self, timeout: float = 2.0) -> bool:
        """Check if the device responds as an RNode.

        Sends ``CMD_DETECT`` with ``DETECT_REQ`` and expects
        ``DETECT_RESP``.

        Args:
            timeout: Seconds to wait for response.

        Returns:
            ``True`` if the device is an RNode.
        """
        try:
            response = self.send_command(CMD_DETECT, [DETECT_REQ], timeout=timeout)
            if response is None:
                return False
            return len(response) > 0 and response[0] == DETECT_RESP
        except Exception as exc:
            print(f"WARNING: RNodeKiss.detect() failed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return False

    def get_firmware_version(self) -> str | None:
        """Read the firmware version string (e.g. "1.73")."""
        response = self.send_command(CMD_FW_VERSION, [0x00])
        if response is None or len(response) < 2:
            return None
        major = response[0]
        minor = response[1]
        patch = response[2] if len(response) > 2 else 0
        return f"{major}.{minor}.{patch}"

    def get_platform(self) -> int | None:
        """Read the platform byte (e.g. ``PLATFORM_ESP32``)."""
        response = self.send_command(CMD_PLATFORM, [0x00])
        if response is None or len(response) == 0:
            return None
        return response[0]

    def get_mcu(self) -> int | None:
        """Read the MCU byte (e.g. ``MCU_ESP32``)."""
        response = self.send_command(CMD_MCU, [0x00])
        if response is None or len(response) == 0:
            return None
        return response[0]

    def get_board(self) -> int | None:
        """Read the board type byte."""
        response = self.send_command(CMD_BOARD, [0x00])
        if response is None or len(response) == 0:
            return None
        return response[0]

    def get_device_hash(self) -> bytes | None:
        """Read the device hash."""
        response = self.send_command(CMD_DEV_HASH, [0x01])
        if response is None:
            return None
        return bytes(response)

    def read_rom(self) -> bytes | None:
        """Read the full EEPROM contents."""
        response = self.send_command(CMD_ROM_READ, [0x00])
        if response is None:
            return None
        return bytes(response)

    def write_rom(self, address: int, value: int) -> None:
        """Write a single byte to EEPROM at *address*.

        Sleeps 85 ms after write to allow the device to commit to EEPROM.
        """
        self.send_kiss_command([CMD_ROM_WRITE, address & 0xFF, value & 0xFF])
        time.sleep(0.085)

    def wipe_rom(self) -> None:
        """Unlock and wipe the EEPROM info section.

        This can take up to 30 seconds.
        """
        self.send_kiss_command([CMD_UNLOCK_ROM, ROM_UNLOCK_BYTE])
        time.sleep(30.0)

    def set_firmware_hash(self, hash_bytes: bytes) -> None:
        """Set the firmware hash via ``CMD_FW_HASH``."""
        self.send_kiss_command([CMD_FW_HASH] + list(hash_bytes))

    def reset(self) -> None:
        """Send a soft-reset command."""
        self.send_kiss_command([CMD_RESET, CMD_RESET_BYTE])


# ---------------------------------------------------------------------------
# Flash and Provision Functions
# ---------------------------------------------------------------------------


def _find_esptool() -> str | None:
    """Return the path to the esptool.py executable, or None.

    Tries ``esptool.py`` (installed via pip) then ``python -m esptool``.
    """
    import shutil

    esptool_path = shutil.which("esptool.py")
    if esptool_path is not None:
        return esptool_path
    # Fallback: check if "python3 -m esptool" works
    python = shutil.which("python3") or shutil.which("python") or sys.executable
    if python is not None:
        try:
            result = subprocess.run(
                [python, "-m", "esptool", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return python  # caller must use `python -m esptool`
        except Exception as exc:
            import sys as _sys
            print(f"DEBUG: esptool fallback detection failed: {exc}", file=_sys.stderr)
    return None


def flash_rnode_firmware(
    port: str, firmware_path: str | None = None, timeout: int = 120
) -> tuple[bool, str]:
    """Flash RNode firmware onto the ESP32 at *port* using esptool.

    If *firmware_path* is not provided, the caller must have set
    ``RNODE_FIRMWARE_PATH`` in the environment or passed a valid path.

    Args:
        port: Device path (e.g. ``/dev/ttyUSB0``).
        firmware_path: Path to the firmware .bin file.  If None, reads
            from the ``RNODE_FIRMWARE_PATH`` environment variable.
        timeout: Seconds to allow for the full flash operation.

    Returns:
        A ``(success, message)`` tuple.
    """
    if firmware_path is None:
        firmware_path = os.environ.get("RNODE_FIRMWARE_PATH", "")
    if not firmware_path or not os.path.isfile(firmware_path):
        msg = (
            f"Firmware file not found: {firmware_path!r}. "
            "Set RNODE_FIRMWARE_PATH env var or pass firmware_path."
        )
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)

    esptool = _find_esptool()
    if esptool is None:
        msg = "esptool.py not found on PATH. Install with: pip install esptool"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)

    # Build the esptool command arguments
    if esptool.endswith("python3") or esptool.endswith("python"):
        base_cmd = [esptool, "-m", "esptool"]
    else:
        base_cmd = [esptool]

    # Step 1: Erase flash
    print(f"\nErasing flash on {port} ...", flush=True)
    try:
        result = subprocess.run(
            base_cmd + ["--port", port, "erase_flash"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"esptool erase_flash timed out after {timeout}s: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except FileNotFoundError:
        msg = f"esptool not found: {esptool}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except Exception as exc:
        msg = f"esptool erase_flash on {port} failed: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)

    if result.returncode != 0:
        stderr_tail = [line for line in result.stderr.strip().split("\n") if line][-5:]
        if stderr_tail:
            msg = "Erase failed: " + "\n".join(stderr_tail)
        else:
            msg = f"esptool erase_flash exited {result.returncode}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)

    print("Erase complete.", flush=True)

    # Step 2: Write firmware
    print(f"Writing firmware to {port} ...", flush=True)
    try:
        result = subprocess.run(
            base_cmd
            + [
                "--port",
                port,
                "--baud",
                "921600",
                "write_flash",
                "0x0",
                firmware_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"esptool write_flash timed out after {timeout}s: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except FileNotFoundError:
        msg = f"esptool not found: {esptool}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except Exception as exc:
        msg = f"esptool write_flash on {port} failed: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)

    if result.returncode != 0:
        stderr_tail = [line for line in result.stderr.strip().split("\n") if line][-5:]
        if stderr_tail:
            msg = "Flash failed: " + "\n".join(stderr_tail)
        else:
            msg = f"esptool write_flash exited {result.returncode}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)

    print("Flash successful.", flush=True)
    return (True, "Flash successful")


def provision_rnode_eeprom(
    port: str,
    product: int = 0x03,
    model: int = 0xAA,
    hw_rev: int = 1,
    serial_num: int | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Provision the RNode EEPROM via KISS commands.

    Writes product, model, hardware revision, serial number, made
    timestamp, MD5 checksum, and a blank 128-byte signature.

    Args:
        port: Device path.
        product: Product code (default ``0x03`` = RNode).
        model: Model code (default ``0xAA`` = Heltec LoRa 32 V3).
        hw_rev: Hardware revision byte.
        serial_num: 32-bit serial number.  If None, uses current Unix time.
        timeout: Seconds to wait for the entire provisioning sequence.

    Returns:
        A ``(success, message)`` tuple.
    """
    if serial_num is None:
        serial_num = int(time.time()) & 0xFFFFFFFF

    print(f"\nProvisioning RNode EEPROM on {port} ...", flush=True)

    try:
        with RNodeKiss(port, timeout=float(timeout)) as rnode:
            # Unlock ROM
            print("  Unlocking ROM ...", flush=True)
            rnode.wipe_rom()

            # Build EEPROM info block: product, model, hw_rev, serial (4), made (4)
            made = int(time.time()) & 0xFFFFFFFF
            info_bytes = (
                bytes(
                    [
                        product & 0xFF,
                        model & 0xFF,
                        hw_rev & 0xFF,
                    ]
                )
                + struct.pack(">I", serial_num)
                + struct.pack(">I", made)
            )

            # Calculate and write checksum
            checksum = ROM.calc_checksum(info_bytes)

            # Write info fields
            print("  Writing EEPROM info fields ...", flush=True)
            for i, byte in enumerate(info_bytes):
                rnode.write_rom(i, byte)

            # Write checksum (16 bytes at ADDR_CHKSUM)
            for i, byte in enumerate(checksum):
                rnode.write_rom(ROM.ADDR_CHKSUM + i, byte)

            # Write blank signature (128 bytes at ADDR_SIGNATURE)
            print("  Writing blank signature ...", flush=True)
            for i in range(128):
                rnode.write_rom(ROM.ADDR_SIGNATURE + i, 0x00)

            # Lock info
            print("  Locking EEPROM info ...", flush=True)
            rnode.write_rom(ROM.ADDR_INFO_LOCK, ROM.INFO_LOCK_BYTE)

            # Verify provisioning
            print("  Verifying EEPROM ...", flush=True)
            rom_data = rnode.read_rom()
            if rom_data is None or len(rom_data) < ROM.ADDR_INFO_LOCK + 1:
                return (False, "EEPROM verification failed: could not read ROM")

            if rom_data[ROM.ADDR_INFO_LOCK] != ROM.INFO_LOCK_BYTE:
                return (False, "EEPROM info lock byte not set after provisioning")

    except ImportError as exc:
        msg = f"pyserial is required for serial communication. Install with: pip install pyserial ({exc})"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except _SerialException as exc:
        msg = f"Serial error during EEPROM provisioning on {port}: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)
    except Exception as exc:
        msg = f"EEPROM provisioning on {port} failed: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)

    print("EEPROM provisioning complete.", flush=True)
    return (True, "EEPROM provisioning complete")


def set_rnode_firmware_hash(
    port: str, hash_bytes: bytes | None = None, timeout: float = 15.0
) -> tuple[bool, str]:
    """Set the firmware hash on the RNode.

    Reads the device hash via ``CMD_DEV_HASH`` and sends it back via
    ``CMD_FW_HASH``.  This is required to clear the "Firmware Corrupt"
    error after fresh flashing.

    Args:
        port: Device path.
        hash_bytes: Firmware hash bytes.  If None, reads the device hash
            and uses that (which effectively marks the current firmware
            as valid).
        timeout: Seconds to wait.

    Returns:
        A ``(success, message)`` tuple.
    """
    print(f"\nSetting firmware hash on {port} ...", flush=True)

    try:
        with RNodeKiss(port, timeout=float(timeout)) as rnode:
            if hash_bytes is None:
                print("  Reading device hash ...", flush=True)
                dev_hash = rnode.get_device_hash()
                if dev_hash is None:
                    return (False, "Failed to read device hash")
                hash_bytes = dev_hash

            print(f"  Writing firmware hash ({len(hash_bytes)} bytes) ...", flush=True)
            rnode.set_firmware_hash(hash_bytes)
    except ImportError as exc:
        msg = f"pyserial is required for serial communication. Install with: pip install pyserial ({exc})"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except _SerialException as exc:
        msg = f"Serial error setting firmware hash on {port}: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)
    except Exception as exc:
        msg = f"Setting firmware hash on {port} failed: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)

    print("Firmware hash set.", flush=True)
    return (True, "Firmware hash set")


def check_rnode_firmware(port: str, timeout: float = 15.0) -> bool:
    """Check whether a device on *port* is running RNode firmware.

    Uses KISS ``CMD_DETECT`` instead of shelling out to ``rnodeconf``.
    No system Python or ``rns`` dependency needed.

    Args:
        port: Device path (e.g. ``/dev/ttyUSB0``).
        timeout: Seconds to wait for the detect response.

    Returns:
        ``True`` if the port responds as an RNode.
    """
    print(f"\nChecking for RNode firmware on {port} ...", flush=True)
    try:
        with RNodeKiss(port, timeout=float(timeout)) as rnode:
            if rnode.detect():
                print("  OK: RNode firmware detected", flush=True)
                return True
            # Fallback: try reading version
            version = rnode.get_firmware_version()
            if version is not None:
                print(f"  OK: RNode firmware v{version} detected", flush=True)
                return True
    except ImportError as exc:
        msg = f"pyserial is required for serial communication. Install with: pip install pyserial ({exc})"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False
    except _SerialException as exc:
        print(f"WARNING: Serial error on {port}: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"WARNING: RNode check on {port} failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False

    print("  RNode firmware not detected", flush=True)
    return False


def flash_rnode(
    port: str,
    firmware_path: str | None = None,
    flash_timeout: int = 120,
    provision: bool = True,
) -> tuple[bool, str]:
    """Full RNode flash workflow: flash → provision EEPROM → set hash → verify.

    This is the main orchestrator that replaces ``rnodeconf --autoinstall``.

    Args:
        port: Device path.
        firmware_path: Path to firmware .bin file.
        flash_timeout: Seconds for the esptool flash step.
        provision: If True, also provision EEPROM and set firmware hash.

    Returns:
        A ``(success, message)`` tuple.
    """
    print(f"\n--- Flashing RNode firmware on {port} ---", flush=True)

    # Step 1: Flash firmware
    success, message = flash_rnode_firmware(port, firmware_path, flash_timeout)
    if not success:
        return (False, f"Flash failed: {message}")

    # Wait for ESP32 to reboot after flash
    print("  Waiting for device to reboot after flash ...", flush=True)
    time.sleep(3.0)

    if not provision:
        print("RNode flash complete (provision skipped).", flush=True)
        return (True, "Flash completed (provision skipped)")

    # Step 2: Provision EEPROM
    success, message = provision_rnode_eeprom(port)
    if not success:
        return (False, f"EEPROM provisioning failed: {message}")

    # Step 3: Set firmware hash
    success, message = set_rnode_firmware_hash(port)
    if not success:
        return (False, f"Firmware hash setting failed: {message}")

    # Step 4: Verify
    print(f"\nVerifying RNode on {port} ...", flush=True)
    if check_rnode_firmware(port):
        print("OK: RNode ready", flush=True)
        return (True, "RNode firmware flashed and provisioned successfully")
    else:
        return (False, "RNode verification failed after flashing")
