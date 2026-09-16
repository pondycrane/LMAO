# µReticulum DTU (RAK3172 P2P) LoRa Interface for Sprout
# LoRa radio via the M5Stack A152 DTU base: STM32WLE5CC in LoRa P2P mode,
# driven over UART AT (AT+PSEND / AT+PRECV / +EVT:RXP2P).
#
# RNode-compatible framing — identical to LoRaInterface in this port
# (see lora.py): 1-byte header per LoRa frame, upper nibble = random
# sequence, bit 0 = FLAG_SPLIT; payloads > 254 B split across exactly 2
# frames (max 508 B). Wire-compatible with the reference RNode firmware
# and the LMAO server's RNode over the same air parameters
# (868 MHz / BW 125 k / SF 7 / CR 4:5 / preamble 24 / syncword 0x1424),
# so the server RNode pairs with Sprout exactly like the Cardputer's SX1262.
#
# The ONLY text hop is the UART AT wire (RUI4 AT+PSEND/PRECV take/return
# hex); over the air the payload is the same binary RNS frame as the
# SX1262 path. Bytes in/out are otherwise untouched.

import contextlib
import gc
import os
import time

from ..log import LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_VERBOSE, log
from . import Interface
from lib.dtu.dtu_at import P2P_RADIO, RAK3172P2P

# MicroPython const() support — no-op under CPython
try:
    from micropython import const
except ImportError:

    def const(x):
        return x  # type: ignore[assignment]


# RNode header constants (matches RNode_Firmware Framing.h)
_FLAG_SPLIT = const(0x01)
_SEQ_MASK = const(0xF0)

# Max payload per LoRa frame (255 - 1 byte RNode header)
_FRAME_PAYLOAD = const(254)

# Reassembly timeout (seconds)
_REASM_TIMEOUT = const(15)

# UART baud between the Atom and the DTU base.
_DTU_BAUD = const(115200)


class DtuInterface(Interface):
    def __init__(self, config):
        name = config.get("name", "DTU P2P LoRa")
        super().__init__(name)

        # Max on-air RNS packet: RNode protocol splits >254 B into 2 frames
        # (max 508 B total). Used for link-MTU clamping at transit.
        self.HW_MTU = 508

        # Atom <-> DTU UART pins (via the 9-pin base stack).
        self._tx_pin = config.get("tx_pin", 22)
        self._rx_pin = config.get("rx_pin", 19)
        self._baud = config.get("baud", _DTU_BAUD)

        # Optional pre-created UART, injected by the caller (e.g. rns_bringup
        # builds it on the clean heap BEFORE RNS construction to dodge heap
        # fragmentation — the PICO-D4 heap is tiny). Same pattern as
        # LoRaInterface's external ``spi``.
        self._uart = config.get("uart", None)

        # Radio parameters (defaults = LMAO mesh params).
        radio = dict(P2P_RADIO)
        for k in ("freq", "sf", "bw", "cr", "ppl", "syncword", "txp"):
            if k in config:
                radio[k] = config[k]

        # Split-packet reassembly state
        self._reasm_buf = None
        self._reasm_seq = None
        self._reasm_time = 0

        try:
            from machine import Pin, UART

            import gc as _gc
            _gc.collect()

            self._uart = UART(
                2,
                baudrate=self._baud,
                tx=Pin(self._tx_pin),
                rx=Pin(self._rx_pin),
                timeout=600,
                rxbuf=256,
        txbuf=256,
            )
            self._radio = RAK3172P2P(self._uart)
            self._radio.configure(cfg=radio, log=None)
            self._radio.rx_start(listen=True, log=None)
            self.online = True
            log(
                "DtuInterface online: "
                + self.name
                + " "
                + str(radio["freq"])
                + " SF"
                + str(radio["sf"])
                + " BW"
                + str(radio["bw"])
                + " CR"
                + str(radio["cr"])
                + " PPL"
                + str(radio["ppl"])
                + " TX"
                + str(radio["txp"])
                + "dBm",
                LOG_NOTICE,
            )
        except Exception as e:
            log("DtuInterface init failed: " + str(e), LOG_ERROR)
            self.online = False

    def process_outgoing(self, data):
        if not self.online or not hasattr(self, "_radio"):
            return False
        try:
            if len(data) > 2 * _FRAME_PAYLOAD:
                log(
                    "DTU drop: " + str(len(data)) + "B exceeds "
                    + str(2 * _FRAME_PAYLOAD),
                    LOG_DEBUG,
                )
                return False

            data = self.ifac_sign(data)

            # RNode-compatible header: random seq in upper nibble.
            header = os.urandom(1)[0] & _SEQ_MASK

            if len(data) > _FRAME_PAYLOAD:
                # Split into 2 frames (RNode protocol)
                header |= _FLAG_SPLIT
                hdr = bytes([header])
                ok1 = self._radio.tx(hdr + data[:_FRAME_PAYLOAD], log=_dtu_dbg)
                self._radio.rx_start(listen=True)
                ok2 = self._radio.tx(hdr + data[_FRAME_PAYLOAD:], log=_dtu_dbg)
                self._radio.rx_start(listen=True)
                ok = ok1 and ok2
                log("DTU TX " + str(len(data)) + "B split seq=" + hex(header >> 4), LOG_DEBUG)
            else:
                # Single frame
                ok = self._radio.tx(bytes([header]) + data, log=_dtu_dbg)
                self._radio.rx_start(listen=True)
                log("DTU TX " + str(len(data)) + "B", LOG_DEBUG)

            if ok:
                self.txb += len(data)
                self.tx += 1
                self._last_activity = time.time()
            return ok
        except Exception as e:
            log("DTU send error: " + str(e), LOG_ERROR)
            with contextlib.suppress(BaseException):
                self._radio.rx_start(listen=True)
            return False

    async def poll_loop(self):
        import uasyncio as asyncio

        log("DTU poll loop started for " + self.name, LOG_NOTICE)

        _last_gc = time.time()
        _last_diag = time.time()
        _rx_pkt_count = 0

        while self.online:
            try:
                now = time.time()

                # Periodic GC
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                # Periodic diagnostics
                if now - _last_diag >= 10:
                    log(
                        "DTU diag: pkts=" + str(_rx_pkt_count),
                        LOG_DEBUG,
                    )
                    _rx_pkt_count = 0
                    _last_diag = now

                # Stale reassembly cleanup
                if self._reasm_buf is not None and now - self._reasm_time > _REASM_TIMEOUT:
                    log("DTU discarding stale split fragment", LOG_DEBUG)
                    self._reasm_buf = None
                    self._reasm_seq = None

                hit = self._radio.poll(timeout_ms=150)
                if hit:
                    payload, rssi, snr = hit
                    self.rssi = rssi
                    self.snr = snr
                    log(
                        "DTU RX raw "
                        + str(len(payload))
                        + "B RSSI="
                        + str(rssi)
                        + " SNR="
                        + str(snr),
                        LOG_DEBUG,
                    )
                    if len(payload) < 2:
                        continue

                    header = payload[0]
                    body = payload[1:]

                    if header & _FLAG_SPLIT:
                        # Split packet — reassemble 2 frames
                        seq = header & _SEQ_MASK
                        if self._reasm_buf is None or self._reasm_seq != seq:
                            if self._reasm_buf is not None:
                                log("DTU split seq mismatch, restarting", LOG_DEBUG)
                            self._reasm_buf = bytearray(body)
                            self._reasm_seq = seq
                            self._reasm_time = time.time()
                            log(
                                "DTU split frame 1: "
                                + str(len(body))
                                + "B seq="
                                + hex(seq >> 4),
                                LOG_DEBUG,
                            )
                            pkt = None
                        else:
                            self._reasm_buf.extend(body)
                            pkt = bytes(self._reasm_buf)
                            self._reasm_buf = None
                            self._reasm_seq = None
                            log(
                                "DTU split frame 2: "
                                + str(len(body))
                                + "B -> "
                                + str(len(pkt))
                                + "B total",
                                LOG_DEBUG,
                            )
                    else:
                        # Non-split packet
                        pkt = body

                    if pkt is not None:
                        _rx_pkt_count += 1
                        log(
                            "DTU recv " + str(len(pkt)) + "B RSSI=" + str(self.rssi)
                            + " SNR=" + str(self.snr),
                            LOG_DEBUG,
                        )
                        self.process_incoming(pkt)
                        gc.collect()

            except Exception as e:
                log("DTU poll error: " + str(e), LOG_ERROR)

            await asyncio.sleep(0.05)

        log("DTU poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        if hasattr(self, "_radio"):
            with contextlib.suppress(BaseException):
                self._radio.rx_start(listen=False)
        log("DtuInterface " + self.name + " closed", LOG_VERBOSE)

    def __str__(self):
        return "DtuInterface[" + self.name + "]"


def _dtu_dbg(msg):
    log(msg, LOG_DEBUG)
