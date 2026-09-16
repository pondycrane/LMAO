"""Sprout RNS + DTU P2P LoRa bring-up / RF-pairing check (increment 2).

Brings up µReticulum with Sprout's identity + the DtuInterface on the LMAO
mesh radio parameters, sends a destination announce over the DTU P2P pipe, and
runs the event loop briefly to exercise TX and RX.

What to look for:
  * ``DTU TX ...B`` logs + the driver's PSEND ACK -> Sprout frames are on air
    with the RNode-compatible header
  * ``IFACE ... tx=N rx=M rssi=.. snr=..`` summary after RUN_SECONDS
  * Any ``DTU RX`` logs / rssi = the server RNode (or another LMAO node) is in
    range and pairing

Run (from smart_irrigation/firmware) with the host dir mounted:
    mpremote connect /dev/ttyUSB0 mount . run tools/rns_bringup.py
"""

import gc
import sys
import time

for _p in ("/", ""):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uasyncio as asyncio

import config as spr
from lib.urns.destination import Destination
from lib.urns.log import LOG_INFO, set_loglevel
from lib.urns.reticulum import Reticulum

RUN_SECONDS = 25


def main():
    print("SPROUT RNS BRING-UP start")
    set_loglevel(LOG_INFO)

    # Stage lightweight imports BEFORE the heavy RNS construction + gc between
    # each heavy step. The PICO-D4 heap is tiny (~25 KB free after urns import)
    # so transient peaks must be collected piecemeal for the DTU interface to
    # fit at setup_interfaces() time.
    from machine import Pin, UART  # noqa: F401  (cache into sys.modules)

    import lib.dtu.dtu_at as _dtu  # noqa: F401  (stage the 4 KB module)

    gc.collect()
    # Pre-create the DTU UART on the CLEAN heap (before RNS construction)
    # and inject it via config — same external-``spi`` pattern as LoRaInterface;
    # avoids a large contiguous UART allocation in a fragmented heap later.
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
    gc.collect()
    rns.setup_interfaces()
    gc.collect()
    print("MEM after interfaces:", gc.mem_free())
    print("Identity: " + rns.identity.hexhash)
    print("Interfaces:", len(rns.interfaces))
    gc.collect()

    gc.collect()
    # Announce Sprout's identity over an IN single destination.
    try:
        d = Destination(rns.identity, Destination.IN, Destination.SINGLE, "lmao", "sprout")
        d.announce()
        print("Announce sent.")
    except Exception as e:
        print("Announce error: " + str(e))
    gc.collect()

    async def run_loop():
        t0 = time.time()
        task_rns = asyncio.create_task(rns.run())
        while time.time() - t0 < RUN_SECONDS:
            await asyncio.sleep(0.5)
        print("--- iface summary ---")
        for ifc in rns.interfaces:
            print(
                "IFACE "
                + str(ifc.name)
                + " online="
                + str(ifc.online)
                + " tx="
                + str(ifc.tx)
                + " rx="
                + str(ifc.rx)
                + " rssi="
                + str(ifc.rssi)
                + " snr="
                + str(ifc.snr)
            )
        task_rns.cancel()
        try:
            await task_rns
        except BaseException:
            pass

    asyncio.run(run_loop())
    print("SPROUT RNS BRING-UP done")


main()
