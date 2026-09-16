"""DTU P2P bring-up probe (engineering step, not a read-only gate probe).

1. Knock: is the RAK3172 AT-responsive?
2. Show the current P2P radio config (read-only)
3. Set LMAO mesh parameters (868 MHz / BW125 / SF7 / CR4:5 / preamble 24 /
   syncword 0x1424 / TX ~17 dBm)
4. Transmit a small binary test frame (hex on the AT wire, binary over air)
5. Enable continuous receive and report anything heard

Run (from smart_irrigation/firmware) with the host dir mounted:
    mpremote connect /dev/ttyUSB0 mount . run tools/dtu_p2p_probe.py
"""
import sys
import time

from machine import Pin, UART

sys.path.append(".")

from lib.dtu import dtu_at  # noqa: E402


def log(msg):
    print("DTU", msg)


def main():
    print("DTU P2P PROBE start")
    uart = UART(2, baudrate=115200, tx=Pin(22), rx=Pin(19), timeout=600, rxbuf=1024)
    radio = dtu_at.RAK3172P2P(uart)

    # 0) knock
    log("AT -> %r" % (radio.at("AT").decode().strip(),))
    log("AT+NWM=? -> %r" % (radio.at("AT+NWM=?").decode().strip(),))

    # 1) current config (read-only)
    radio.query_all(log)

    # 2) set the LMAO mesh parameters
    print("--- configure to LMAO mesh params ---")
    radio.configure(log=log)

    # 3) verify what the DTU reports now
    print("--- re-query ---")
    radio.query_all(log)

    # 4) transmit a small binary test frame
    print("--- TX test ---")
    test = b"SPROUTP2P\x00\x01\x02"
    ok = radio.tx(test, log)
    print("TX accepted:", ok)

    # 5) start RX and listen briefly
    print("--- RX ---")
    radio.rx_start(listen=True, log=log)
    hit = radio.poll(timeout_ms=2500)
    if hit:
        payload, rssi, snr = hit
        print("RX heard payload=%s rssi=%s snr=%s" % (payload.hex(), rssi, snr))
    else:
        print("RX heard: (nothing in 2.5s)")

    print("DTU P2P PROBE done")


main()
