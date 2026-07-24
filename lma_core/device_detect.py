"""Shared USB device detection for LMAO hardware.

Single source of truth for identifying Cardputer and RNode devices
connected via USB.  Uses VID/PID as primary identification with exact
product/manufacturer strings as secondary confirmation.  No broad
keyword fallback matching — this prevents cross-matching that
misidentified the Heltec RNode (CP2102, VID 0x10C4) as a Cardputer.

Verified real-device fingerprints:
  - Cardputer (M5Stack Cardputer ADV, ESP32-S3 native USB-Serial-JTAG):
    /dev/ttyACM0, VID 0x303A  PID 0x8120,
    product=M5Stack UiFlow 2.0,
    manufacturer=M5Stack Technology Co., Ltd
  - RNode (Heltec ESP32 LoRa, CP2102 UART bridge):
    /dev/ttyUSB0, VID 0x10C4  PID 0xEA60,
    product=CP2102 USB to UART Bridge Controller,
    manufacturer=Silicon Labs

API
---

  result = detect_devices()
  # result.cardputer     → DeviceInfo | None
  # result.rnode         → DeviceInfo | None
  # result.cardputer_port   → str | None      (device path)
  # result.rnode_port       → str | None
  # result.all_ports        → list[DeviceInfo]

  port = find_cardputer_port(preferred=None)   → str | None
  port = find_rnode_port(preferred=None)       → str | None

Optional protocol-level probes (short timeouts, never hang):
  ok = probe_rnode(port)           → bool   (DETECT command)
  ok = probe_cardputer_repl(port)  → bool   (MicroPython raw REPL)

Protocol probe reference:
  - RNode DETECT: send 0xC0 0x08 0x73 0xC0, expect 0xC0 0x08 0x46 0xC0
  - Cardputer REPL: send Ctrl+C×2 + Ctrl+A, look for "raw REPL" banner
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Verified device fingerprints
# ---------------------------------------------------------------------------

# fmt: off
_CARDCOMPUTER_FINGERPRINTS: tuple[tuple[int, int, dict[str, str]], ...] = (
    (
        0x303A,  # VID — Espressif (ESP32-S3 native USB-Serial-JTAG)
        0x8120,  # PID — M5Stack UiFlow 2.0
        {
            "product":      "M5Stack UiFlow 2.0",
            "manufacturer": "M5Stack Technology Co., Ltd",
        },
    ),
)

_RNODE_FINGERPRINTS: tuple[tuple[int, int, dict[str, str]], ...] = (
    (
        0x10C4,  # VID — Silicon Labs
        0xEA60,  # PID — CP2102 USB to UART Bridge Controller
        {
            "product":      "CP2102 USB to UART Bridge Controller",
            "manufacturer": "Silicon Labs",
        },
    ),
)
# fmt: on

# Keyword strings used for secondary confirmation when VID/PID match
# but product/manufacturer strings are unavailable.
_CARDCOMPUTER_DESC_KEYWORDS: tuple[str, ...] = ("cardputer", "m5stack")
_RNODE_DESC_KEYWORDS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DeviceInfo:
    """Information about a detected serial device."""

    port: str
    """Device path (e.g. ``/dev/ttyACM0``)."""

    vid: int | None = None
    """USB Vendor ID."""

    pid: int | None = None
    """USB Product ID."""

    product: str | None = None
    """USB product string."""

    manufacturer: str | None = None
    """USB manufacturer string."""

    serial: str | None = None
    """USB serial number."""

    description: str | None = None
    """USB device description."""


@dataclass
class DetectionResult:
    """Structured result of device detection."""

    cardputer: DeviceInfo | None = None
    """Detected Cardputer device, or ``None``."""

    rnode: DeviceInfo | None = None
    """Detected RNode device, or ``None``."""

    confidence: dict[str, str] = field(default_factory=dict)
    """Confidence levels: ``{"cardputer": "high"|"medium"|"low", ...}``."""

    all_ports: list[DeviceInfo] = field(default_factory=list)
    """All discovered serial ports (USB and non-USB)."""

    @property
    def cardputer_port(self) -> str | None:
        """Convenience: device path of the Cardputer, or ``None``."""
        return self.cardputer.port if self.cardputer else None

    @property
    def rnode_port(self) -> str | None:
        """Convenience: device path of the RNode, or ``None``."""
        return self.rnode.port if self.rnode else None


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------


def _read_port_info(p) -> DeviceInfo:
    """Extract vendor/product/serial/description from a ``list_ports`` entry.

    Uses ``getattr`` for safety — some platforms or old pyserial versions
    may not have all attributes.
    """
    return DeviceInfo(
        port=p.device,
        vid=getattr(p, "vid", None),
        pid=getattr(p, "pid", None),
        product=(getattr(p, "product", None) or "").strip() or None,
        manufacturer=(getattr(p, "manufacturer", None) or "").strip() or None,
        serial=(getattr(p, "serial_number", None) or "").strip() or None,
        description=(getattr(p, "description", None) or "").strip() or None,
    )


def _match_fingerprint(
    info: DeviceInfo,
    fingerprints: tuple[tuple[int, int, dict[str, str]], ...],
) -> str:
    """Check *info* against known fingerprints.

    Returns a confidence level: ``"high"``, ``"medium"``, or ``""`` (no match).

    Confidence levels:
    - ``"high"``   — VID + PID match AND product/manufacturer strings
                     match exactly (or are unavailable on this OS).
    - ``"medium"`` — VID + PID match but product/manufacturer strings
                     are present and do NOT match the expected values.
    """
    if info.vid is None or info.pid is None:
        return ""

    for vid, pid, strings in fingerprints:
        if info.vid != vid or info.pid != pid:
            continue

        # VID/PID match — now check product/manufacturer strings
        prod = info.product or ""
        manu = info.manufacturer or ""
        exp_prod = strings.get("product", "")
        exp_manu = strings.get("manufacturer", "")

        # If product/manufacturer are both unavailable (e.g. platform
        # doesn't expose USB strings), we can't downgrade confidence.
        if not prod and not manu:
            return "high"

        # If expected strings match (or are also empty), high confidence
        prod_match = (prod.lower() == exp_prod.lower()) or not exp_prod
        manu_match = (manu.lower() == exp_manu.lower()) or not exp_manu

        if prod_match and manu_match:
            return "high"

        # VID/PID matches but product/manufacturer differs → medium
        return "medium"

    return ""


def _desc_matches_any(info: DeviceInfo, keywords: tuple[str, ...]) -> bool:
    """Check if *info.description* (or *product*) contains any keyword."""
    text = " ".join(
        part.lower()
        for part in (info.description, info.product, info.manufacturer)
        if part
    )
    return any(kw in text for kw in keywords)


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def detect_devices() -> DetectionResult:
    """Detect and classify all connected USB serial devices.

    Scans all serial ports via ``serial.tools.list_ports.comports()``,
    classifies each as Cardputer or RNode using VID/PID + product strings
    from verified real-device fingerprints.

    Secondary confirmation via description keywords is only used to
    resolve VID/PID collisions (e.g. two different devices that share
    the same VID/PID).  No broad keyword-only matching is performed.

    Returns:
        ``DetectionResult`` with ``cardputer``, ``rnode``, and ``all_ports``.
    """
    result = DetectionResult()

    try:
        import serial.tools.list_ports
    except ImportError:
        return result

    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception:
        return result

    cards: list[tuple[str, DeviceInfo]] = []  # (confidence, info)
    rnodes: list[tuple[str, DeviceInfo]] = []

    for p in ports:
        info = _read_port_info(p)
        result.all_ports.append(info)

        cp_conf = _match_fingerprint(info, _CARDCOMPUTER_FINGERPRINTS)
        rn_conf = _match_fingerprint(info, _RNODE_FINGERPRINTS)

        # Both fingerprints can theoretically match the same port if
        # there is a VID/PID collision.  Use description keywords to
        # disambiguate.
        if cp_conf and rn_conf:
            if _desc_matches_any(info, _CARDCOMPUTER_DESC_KEYWORDS):
                rn_conf = ""
            elif _desc_matches_any(info, _RNODE_DESC_KEYWORDS):
                cp_conf = ""
            # If still ambiguous, neither gets it (safe fallback)

        if cp_conf:
            cards.append((cp_conf, info))
        if rn_conf:
            rnodes.append((rn_conf, info))

    # Pick best match for each type (prefer high confidence)
    cards.sort(key=lambda x: (0 if x[0] == "high" else 1 if x[0] == "medium" else 2))
    rnodes.sort(key=lambda x: (0 if x[0] == "high" else 1 if x[0] == "medium" else 2))

    if cards:
        conf, info = cards[0]
        result.cardputer = info
        result.confidence["cardputer"] = conf

    if rnodes:
        conf, info = rnodes[0]
        result.rnode = info
        result.confidence["rnode"] = conf

    return result


def find_cardputer_port(preferred: str | None = None) -> str | None:
    """Return the device path of a detected Cardputer.

    When *preferred* is given it is returned immediately (caller-supplied
    port override).  Otherwise all serial ports are scanned using
    :func:`detect_devices`.

    Args:
        preferred: Caller-supplied port override (returned as-is).

    Returns:
        Device path (e.g. ``/dev/ttyACM0``) or ``None``.
    """
    if preferred:
        return preferred

    d = detect_devices()
    if d.cardputer:
        return d.cardputer.port
    return None


def find_rnode_port(preferred: str | None = None) -> str | None:
    """Return the device path of a detected RNode.

    When *preferred* is given it is returned immediately (caller-supplied
    port override).  Otherwise all serial ports are scanned using
    :func:`detect_devices`.

    Args:
        preferred: Caller-supplied port override (returned as-is).

    Returns:
        Device path (e.g. ``/dev/ttyUSB0``) or ``None``.
    """
    if preferred:
        return preferred

    d = detect_devices()
    if d.rnode:
        return d.rnode.port
    return None


# ---------------------------------------------------------------------------
# Optional protocol-level probes
# ---------------------------------------------------------------------------


def probe_rnode(port: str, timeout: float = 0.5, attempts: int = 3) -> bool:
    """Check whether *port* is running RNode firmware using the DETECT protocol.

    Opens *port* at 115200 baud, sends the standard RNode DETECT command
    (``0xC0 0x08 0x73 0xC0``), and checks for a valid DETECT response
    (``0xC0 0x08 0x46 0xC0``) in the reply.

    The probe retries up to *attempts* times, flushing the input buffer
    before each attempt.  This tolerates asynchronous KISS frames (e.g.
    LoRa RX packets) that may interleave with the DETECT response when
    the radio is active.

    Uses short timeouts — this probe will never hang.

    Args:
        port: Serial device path (e.g. ``/dev/ttyUSB0``).
        timeout: Read timeout in seconds (default 0.5).
        attempts: Number of DETECT attempts (default 3).

    Returns:
        ``True`` if the port responds as an RNode.
    """
    try:
        import serial as _serial

        ser = _serial.Serial(port, 115200, timeout=timeout)
        try:
            time.sleep(0.3)
            for _ in range(max(1, attempts)):
                ser.reset_input_buffer()
                ser.write(bytes([0xC0, 0x08, 0x73, 0xC0]))
                time.sleep(0.3)
                data = ser.read(100)

                if (
                    len(data) >= 4
                    and data[0:1] == b"\xC0"
                    and data[1] == 0x08
                    and data[2] == 0x46
                ):
                    return True
            return False
        finally:
            ser.close()
    except Exception:
        return False


def probe_cardputer_repl(port: str, timeout: float = 2.0) -> bool:
    """Check whether *port* is a Cardputer running MicroPython.

    Opens *port* at 115200 baud, attempts to enter MicroPython raw REPL
    (Ctrl+C×2 + Ctrl+A), and checks for the ``raw REPL`` banner.

    Uses short timeouts — this probe will never hang.

    Args:
        port: Serial device path (e.g. ``/dev/ttyACM0``).
        timeout: Overall timeout in seconds (default 2.0).

    Returns:
        ``True`` if the port responds with a MicroPython raw REPL banner.
    """
    try:
        import serial as _serial

        ser = _serial.Serial(port, 115200, timeout=min(timeout, 2.0))
        time.sleep(0.3)
        ser.reset_input_buffer()

        # Ctrl+C twice to interrupt
        ser.write(b"\r\x03\x03")
        time.sleep(0.3)
        ser.read(ser.in_waiting)

        # Ctrl+A to enter raw REPL
        ser.write(b"\r\x01")
        time.sleep(0.3)

        data = ser.read(256)
        ser.close()

        combined = data.lower()
        return b"raw repl" in combined or b"micropython" in combined
    except Exception:
        return False
