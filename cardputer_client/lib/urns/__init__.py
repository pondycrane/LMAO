# µReticulum - MicroPython port of the Reticulum Network Stack
# For ESP32-S3 / Raspberry Pi Pico W

__version__ = "0.1.0"

from . import bz2dec, const, lxmf
from .destination import Destination
from .identity import Identity
from .link import Link
from .log import (
    LOG_CRITICAL,
    LOG_DEBUG,
    LOG_ERROR,
    LOG_EXTREME,
    LOG_INFO,
    LOG_NONE,
    LOG_NOTICE,
    LOG_VERBOSE,
    LOG_WARNING,
    log,
)
from .packet import Packet, PacketReceipt
from .reticulum import Reticulum
from .transport import Transport


def hexrep(data, delimit=True):
    try:
        iter(data)
    except TypeError:
        data = [data]
    d = ":" if delimit else ""
    return d.join(f"{c:02x}" for c in data)
