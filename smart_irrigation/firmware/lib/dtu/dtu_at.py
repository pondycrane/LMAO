# Sprout — RAK3172 P2P-mode AT driver
"""UART-AT driver for the M5Stack A152 DTU base (RAK3172 / STM32WLE5CC) in
LoRa P2P mode (``AT+NWM=0``).

Provides P2P radio configuration, binary transmit (hex-encoded on the AT
wire — the only text hop; the air payload and the urns frames are binary),
and receive primitives. Used by the urns ``dtu.py`` RNS interface and by
engineering tools.

NOTE: this module intentionally *configures* the DTU's P2P radio (sets the
LMAO mesh parameters). That is a deliberate bring-up step, not a read-only
probe — never call it from probe_hardware.py or any gate.

LMAO mesh radio parameters (must match the server-side RNode):
  868 MHz, BW 125 kHz, SF 7, CR 4:5, preamble 24, syncword 0x1424, TX ~17 dBm
"""

import time


# LMAO mesh radio parameters as RUI4 P2P values.
P2P_RADIO = {
    "freq": 868_000_000,  # AT+PFREQ (Hz)
    "sf": 7,  # AT+PSF  spreading factor
    "bw": 0,  # AT+PBW  0 = 125 kHz
    "cr": 0,  # AT+PCR  0 = 4/5
    "ppl": 24,  # AT+PPL  preamble symbols
    "syncword": "1424",  # AT+SYNCWORD (hex, no 0x) -> 0x1424
    "txp": 17,  # AT+PTP  TX power dBm
}

# Ordered P2P configuration commands (query + set).
_P2P_STEPS = (
    ("AT+PFREQ", lambda c: str(c["freq"])),
    ("AT+PSF", lambda c: str(c["sf"])),
    ("AT+PBW", lambda c: str(c["bw"])),
    ("AT+PCR", lambda c: str(c["cr"])),
    ("AT+PPL", lambda c: str(c["ppl"])),
    ("AT+SYNCWORD", lambda c: c["syncword"]),
    ("AT+PTP", lambda c: str(c["txp"])),
)

# Commands probed in order of likelihood of being supported (RUI4).
_QUERIES = (
    "AT+NWM",
    "AT+PFREQ",
    "AT+PSF",
    "AT+PBW",
    "AT+PCR",
    "AT+PPL",
    "AT+PTP",
    "AT+SYNCWORD",
)


class RAK3172P2P:
    """Minimal RUI4 P2P AT client over the Atom <-> DTU UART."""

    def __init__(self, uart):
        self.u = uart

    # -- low-level AT ---------------------------------------------------------

    def _read(self, timeout_ms=1200):
        """Read AT response until a terminal keyword or timeout."""
        data = b""
        deadline = time.ticks_ms() + timeout_ms
        while time.ticks_ms() < deadline:
            if self.u.any():
                data += self.u.read(self.u.any())
            if b"OK" in data or b"ERROR" in data or b"ERR" in data:
                break
            time.sleep_ms(20)
        return data

    def at(self, cmd, timeout_ms=1200):
        """Send an AT command (already including '='), return raw reply bytes."""
        self.u.write(cmd.encode("ascii") + b"\r\n")
        return self._read(timeout_ms)

    # -- inspection -----------------------------------------------------------

    def query_all(self, log=None):
        """Read current P2P radio config (read-only)."""
        out = {}
        for q in _QUERIES:
            resp = self.at(q + "=?", timeout_ms=900).decode().strip()
            out[q] = resp
            if log:
                log(f"  {q}=? -> {resp!r}")
        return out

    # -- configuration --------------------------------------------------------

    def configure(self, cfg=None, log=None):
        """Set the P2P radio to the LMAO mesh parameters (or ``cfg``)."""
        cfg = cfg or P2P_RADIO
        ok = True
        for cmd, valget in _P2P_STEPS:
            value = valget(cfg)
            resp = self.at(f"{cmd}={value}", timeout_ms=900)
            text = resp.decode().strip()
            if log:
                log(f"  {cmd}={value} -> {text!r}")
            if b"AT_MODE_NO_SUPPORT" in resp or b"AT_ERROR" in resp or b"ERROR" in resp:
                ok = False
                if log:
                    log(f"    (note: {cmd} not accepted; continuing)")
        return ok

    # -- TX / RX --------------------------------------------------------------

    def _drain(self, timeout_ms=60):
        """Discard any buffered UART input (stray +EVT lines, etc.)."""
        deadline = time.ticks_ms() + timeout_ms
        while time.ticks_ms() < deadline:
            if self.u.any():
                self.u.read(self.u.any())
            time.sleep_ms(10)

    def tx(self, payload, log=None):
        """Transmit *payload* (bytes) over P2P. Returns True if the DTU ACKed.

        RUI4 P2P requires RX to be OFF while transmitting (``AT+PRECV=0``
        before ``AT+PSEND``); re-enabling RX is the caller's job. The late
        ``+EVT:TXP2P DONE`` event is drained so it can't pollute the next
        read.
        """
        if len(payload) > 250:
            raise ValueError(f"P2P payload too large: {len(payload)} B (max ~250)")
        # Disable RX first (module busy/AT_BUSY_ERROR if left in RX).
        self.at("AT+PRECV=0", timeout_ms=900)
        time.sleep_ms(50)
        self._drain()
        # Write the AT line in chunks so no single large contiguous string is
        # needed (the PICO-D4 heap is tiny + fragmented at send time).
        self.u.write(b"AT+PSEND=")
        for i in range(0, len(payload), 32):
            self.u.write(payload[i : i + 32].hex().encode())
        self.u.write(b"\r\n")
        resp = self._read(3000)
        if log:
            log(f"  TX {len(payload)}B -> {resp.decode().strip()!r}")
        self._drain(200)  # swallow the late +EVT:TXP2P DONE / OK
        ok = b"+EVT:TXP2P DONE" in resp or (
            b"OK" in resp and b"ERROR" not in resp and b"+EVT:TXP2P FAIL" not in resp
        )
        return ok

    def rx_start(self, listen=True, log=None):
        """Enable (continuous) or disable P2P receive. Idempotent."""
        self.at("AT+PRECV=0", timeout_ms=900)
        time.sleep_ms(60)
        self._drain()
        resp = b""
        if listen:
            resp = self.at("AT+PRECV=65535", timeout_ms=1200)
            if log:
                log(f"  AT+PRECV=65535 -> {resp.decode().strip()!r}")
            self._drain(200)
        return b"OK" in resp and b"ERROR" not in resp

    def poll(self, timeout_ms=2000):
        """Wait for a P2P RX event; return (payload_bytes, rssi, snr) or None."""
        data = self._read(timeout_ms)
        txt = data.decode()
        if "+EVT:RXP2P" not in txt:
            return None
        rssi = snr = None
        head = txt.find("+EVT:RXP2P")
        tail = txt[head:]
        # RSSI / SNR appear inside the event line; payload is the trailing hex.
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if lines:
            try:
                rssi = lines[0].split("RSSI")[1].split(",")[0].strip()
            except Exception:
                pass
            try:
                snr = lines[0].split("SNR")[1].split("\r")[0].split("\n")[0].strip()
            except Exception:
                pass
        payload = b""
        for ln in reversed(lines):
            candidate = ln.split(" ")[-1].strip()
            if len(candidate) >= 2 and all(
                ch in "0123456789abcdefABCDEF" for ch in candidate
            ):
                try:
                    payload = bytes.fromhex(candidate)
                    break
                except ValueError:
                    continue
        return payload, rssi, snr
