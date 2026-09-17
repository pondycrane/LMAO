"""Sprout on-node sender — increment 3 (issue #118/#115/#110).

Reads the ENV III air temperature (SHT30 on DTU base Port A, G21/G25), wraps it
in an LMAOEnvelope SensorReport (sensor_id=3 air temp, + humidity id=2), and
sends it to the LMAO server over the DTU P2P LoRa link via LXMF/urns.

This is the on-node equivalent of the Cardputer's sensor sender. It exercises
the same path the production Sprout firmware (main.py) will use:
    SHT30 -> encode_sensor_envelope -> LXMRouter.send_message (Title=p:Envelope)
         -> urns -> DtuInterface -> AT+PSEND -> DTU LoRa -> server RNode
                -> LXMF handler -> NATS -> DuckDB (ingest is #127-fixed)

Run (device flash layout; urns are .mpy via tools/build_mpy.sh):
    mpremote connect /dev/ttyUSB0 run tools/sprout_send_temp.py

Memory notes (ESP32-PICO-D4): keep the RMS staging tight (pre-created UART
injected via config), import LXMF lazily, gc between heavy steps.
"""

import gc
import sys
import time

for _p in ("/", ""):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Module-level urns import (heavy) so RNS construction is cheap and the DTU
# interface fits after it — same staging as rns_bringup. LXMF/encoder stay
# deferred until AFTER interfaces (see main).
from lib.urns.reticulum import Reticulum

import config as spr
from machine import Pin, UART

DEST_HASH_HEX = getattr(spr, "DEST_HASH", None) or "dad35b80164b25f7b1474be86e443702"
RUN_SECONDS = getattr(spr, "SEND_RUN_SECONDS", 90)


def read_sht30():
    from machine import SoftI2C

    i2c = SoftI2C(scl=Pin(21), sda=Pin(25), freq=100000)
    i2c.writeto(0x44, b"\x2c\x06")
    time.sleep_ms(60)
    d = i2c.readfrom(0x44, 6)
    t = -45 + 175 * (((d[0] << 8) | d[1]) / 65535)
    h = 100 * (((d[3] << 8) | d[4]) / 65535)
    return t, h


def main():
    print("SPROUT SEND-TEMP start")
    gc.collect()

    # Stage lightweight imports + pre-create the DTU UART on a clean heap
    # (same pattern as rns_bringup; the PICO-D4 heap is tiny).
    import lib.dtu.dtu_at as _dtu  # noqa: F401

    uart = UART(
        2,
        baudrate=115200,
        tx=Pin(22),
        rx=Pin(19),
        timeout=600,
        rxbuf=256,
        txbuf=256,
    )
    for iface in spr.CONFIG.get("interfaces", []):
        if iface.get("type") == "DtuInterface":
            iface["uart"] = uart
    gc.collect()
    print("MEM before RNS:", gc.mem_free())

    rns = Reticulum(loglevel=3, config_path="/rns/config.json")
    gc.collect()
    print("MEM after RNS:", gc.mem_free())
    rns.config = spr.CONFIG
    rns.setup_interfaces()
    gc.collect()
    print("MEM after interfaces:", gc.mem_free())
    print("Identity:", rns.identity.hexhash)
    print("Interfaces:", len(rns.interfaces))

    # Read ENV III air temp + humidity (with a #124-style single retry).
    t = h = None
    for attempt in range(2):
        try:
            t, h = read_sht30()
            break
        except Exception as e:
            print("SHT30 read fail (%s), retry..." % type(e).__name__ if attempt == 0 else "still failing")
            time.sleep(0.5)
    if t is None:
        print("FATAL: no ENV III reading (Port A contact? #124)")
        return
    print("AIR temp_c=%.2f humidity_pct=%.2f" % (t, h))

        # Build the LMAOEnvelope SensorReport (sensor_id 3 = air temp, 2 = humidity).
    # Import the slim send-only LXMF + encoder now (after interfaces) — it is
    # a reduced overlay (urns_overlay/lxmf.py), small enough for the PICO-D4.
    from lib.proto.lma_encoder import encode_sensor_envelope

    ts = int(time.time() * 1000)
    seq = int(time.time()) % 100000
    readings = [
        (3, t, "C", ts),  # air temperature
        (2, h, "%", ts),  # humidity
    ]
    envelope = encode_sensor_envelope(rns.identity.hexhash, seq, 0.0, readings)
    print("envelope bytes=%d head=%s" % (len(envelope), envelope.hex()[:48]))
    # Reclaim the encoder module's RAM (~5 KB) before the LXMF import.
    import sys as _sys

    _sys.modules.pop("lib.proto.lma_encoder", None)
    gc.collect()
    from lib.urns.lxmf import send_opportunistic

    gc.collect()

    dest = bytes.fromhex(DEST_HASH_HEX)
    res = send_opportunistic(
        destination_hash=dest,
        identity=rns.identity,
        content=envelope,
        title=b"p:Envelope",
    )
    print("send_opportunistic ->", res)
# Run the event loop so urns TX actually fires (deferred LXMF waits for the
    # server path if not yet learned) and the DtuInterface poll_loop runs.
    async def run_loop():
        import uasyncio as asyncio

        t0 = time.time()
        task = asyncio.create_task(rns.run())
        while time.time() - t0 < RUN_SECONDS:
            await asyncio.sleep(0.5)
        print("--- iface summary ---")
        for ifc in rns.interfaces:
            print(
                "IFACE %s online=%s tx=%d rx=%d rssi=%s snr=%s"
                % (ifc.name, ifc.online, ifc.tx, ifc.rx, ifc.rssi, ifc.snr)
            )
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    try:
        asyncio.run(run_loop())
    except Exception as e:
        print("event loop error:", type(e).__name__, e)
    print("SPROUT SEND-TEMP done")


main()
