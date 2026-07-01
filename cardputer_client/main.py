"""
LMAO Cardputer Client — µReticulum LoRa sender/receiver.

Runs on M5Stack Cardputer ADV with onboard LoRa antenna.
Sends periodic "Hello" messages and displays received replies.
"""

import time
import sys

# MicroPython hardware imports
try:
    from machine import Pin, SPI
    import st7789  # Cardputer display driver
    HAS_DISPLAY = True
except ImportError:
    HAS_DISPLAY = False

# µReticulum imports (MicroPython port of RNS)
try:
    import ureticulum as RNS
    import ulxmf as LXMF
except ImportError:
    # Fallback: µReticulum may be installed as 'RNS' on MicroPython
    try:
        import RNS
        import LXMF
    except ImportError:
        RNS = None
        LXMF = None

import config

# ---- Display helpers ----

def init_display():
    """Initialize the Cardputer ST7789 display."""
    if not HAS_DISPLAY:
        return None

    try:
        # Cardputer uses VSPI for display
        spi = SPI(1, baudrate=40000000, polarity=1, phase=0,
                  sck=Pin(36), mosi=Pin(35), miso=Pin(37))
        tft = st7789.ST7789(
            spi, 240, 135,
            reset=Pin(33, Pin.OUT),
            dc=Pin(34, Pin.OUT),
            cs=Pin(12, Pin.OUT),
            backlight=Pin(38, Pin.OUT),
            rotation=1,
        )
        tft.init()
        tft.fill(0x0000)  # Black
        return tft
    except Exception as e:
        print(f"Display init failed: {e}")
        return None


def display_status(tft, lines):
    """Write status lines to the Cardputer screen."""
    if tft is None:
        return
    tft.fill(0x0000)
    y = 5
    for line in lines[:10]:  # Max 10 lines at 13px each
        tft.text(line, 5, y, 0xFFFF)  # White text
        y += 13


def log(msg, tft=None, status_lines=None):
    """Print to serial and optionally update display."""
    print(msg)
    if status_lines is not None:
        status_lines.append(msg)
        if len(status_lines) > 8:
            status_lines.pop(0)
        if tft is not None:
            display_status(tft, status_lines)


# ---- LXMF message handler ----

def handle_reply(message):
    """Callback invoked when an LXMF reply is received."""
    try:
        title = message.title_as_string() if hasattr(message, 'title_as_string') else ""
        content = message.content_as_string() if hasattr(message, 'content_as_string') else str(message.content)
        print(f"\n>>> REPLY from server: {content}")
        # Update global reply buffer
        global last_reply
        last_reply = content
    except Exception as e:
        print(f"handle_reply error: {e}")


last_reply = None


# ---- Main ----

def main():
    global last_reply

    status_lines = []

    # Init display
    tft = init_display()
    log("LMAO Cardputer Client — Booting...", tft, status_lines)

    # Check µReticulum availability
    if RNS is None:
        log("ERROR: µReticulum not installed!", tft, status_lines)
        log("Install ureticulum + ulxmf on your Cardputer.", tft, status_lines)
        while True:
            time.sleep(1)

    # Initialize Reticulum
    log("Init Reticulum...", tft, status_lines)
    reticulum = RNS.Reticulum(config=config.config)
    log("Reticulum OK.", tft, status_lines)

    # Create/load identity
    identity = RNS.Identity()
    identity_hex = RNS.hexrep(identity.hash, delimit=False)
    log(f"ID: {identity_hex[:16]}...", tft, status_lines)

    # Start LXMF router
    router = LXMF.LXMRouter(identity=identity, storagepath="/flash/lxmf_state")
    router.register_delivery_callback(handle_reply)
    log("LXMF router started.", tft, status_lines)

    # ---- Discovery: announce ourselves so server can find us ----
    log("Announcing presence...", tft, status_lines)
    router.announce()
    log("POC Ready.", tft, status_lines)

    # Display "LMAO POC Ready" prominently
    if tft is not None:
        display_status(tft, ["LMAO POC Ready", f"ID: {identity_hex[:24]}"])

    # ---- Main loop: periodic hello + listen for replies ----
    seq = 0
    SERVER_IDENTITY_HASH = None  # Set this to the server's 16-byte hash if known

    while True:
        try:
            seq += 1

            # If we know the server's identity, send a hello message
            if SERVER_IDENTITY_HASH is not None:
                server_id = RNS.Identity.recall(SERVER_IDENTITY_HASH)
                if server_id is not None:
                    hello_text = f"Hello from Cardputer — seq {seq}"
                    msg = LXMF.LXMessage(
                        destination=server_id,
                        source=identity,
                        content=hello_text.encode("utf-8"),
                        title="p:Envelope",
                        desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                    )
                    router.handle_outbound(msg)
                    log(f"Sent: {hello_text}", tft, status_lines)

            # Also send as an opportunistic broadcast (server hears all LoRa packets)
            # This is the POC approach: no need to know the server's hash
            if SERVER_IDENTITY_HASH is None:
                broadcast_text = f"Hello from Cardputer — seq {seq}"
                # Create a broadcast LXMF message
                # In the POC, we send to a broadcast-like destination
                msg = LXMF.LXMessage(
                    destination=None,  # Broadcast / opportunistic
                    source=identity,
                    content=broadcast_text.encode("utf-8"),
                    title="p:Envelope",
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                router.handle_outbound(msg)
                log(f"Sent broadcast: {broadcast_text}", tft, status_lines)

            # Display last reply if any
            if last_reply is not None:
                log(f"Reply: {last_reply}", tft, status_lines)
                last_reply = None

            # Wait before next cycle (10 seconds for demo)
            time.sleep(10)

        except KeyboardInterrupt:
            log("Shutting down...", tft, status_lines)
            break
        except Exception as e:
            log(f"Loop error: {e}", tft, status_lines)
            time.sleep(5)


# Auto-run when flashed to Cardputer
if __name__ == "__main__":
    main()
