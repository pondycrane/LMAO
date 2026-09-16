# µReticulum - MicroPython port of the Reticulum Network Stack
# For ESP32-S3 / Raspberry Pi Pico W

__version__ = "0.1.0"

# NOTE (2026-09-16): the heavy modules (lxmf, link, bz2dec, resource) are no
# longer imported eagerly by this package. Consumers import them directly
# (e.g. ``from urns.lxmf import LXMRouter``, which is what the Cardputer does),
# so they load lazily on first use. This slashes the baseline heap for
# small-heap devices such as Sprout (ESP32-PICO-D4) — the Cardputer is
# unaffected (it never relied on eager package-level attributes).
from . import const
from .destination import Destination
from .identity import Identity
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
