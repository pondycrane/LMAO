"""
LMAO Cardputer Client — µReticulum LoRa sender/receiver.

Runs on M5Stack Cardputer ADV with onboard LoRa antenna.
Sends periodic "Hello" messages and displays received replies.
"""

import time

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
from proto.lma_encoder import make_poc_message


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
    """Write status lines to the Cardputer screen.

    Returns tft on success, or None if display hardware has failed
    (so the caller can disable future display calls).
    """
    if tft is None:
        return None
    try:
        tft.fill(0x0000)
        y = 5
        for line in lines[:10]:  # Max 10 lines at 13px each
            tft.text(line, 5, y, 0xFFFF)  # White text
            y += 13
        return tft
    except Exception as e:
        print(f"Display error: {e} — disabling display")
        return None


def log(msg, tft=None, status_lines=None):
    """Print to serial and optionally update display."""
    print(msg)
    if status_lines is not None:
        status_lines.append(msg)
        if len(status_lines) > 8:
            status_lines.pop(0)
        if tft is not None:
            tft = display_status(tft, status_lines)
    return tft


# ---- LXMF message handler ----

# NOTE: This callback may be invoked from a separate execution context
# (thread, uasyncio task, or interrupt handler). The reply is buffered
# in `pending_replies` for the main loop to drain.
def handle_reply(message):
    """Callback invoked when an LXMF reply is received."""
    # Extract title with individual error handling
    try:
        title = message.title_as_string() if hasattr(message, 'title_as_string') else ""
    except Exception as e:
        print(f"handle_reply: title extraction failed: {e}")
        import sys as _sys
        _sys.print_exception(e)
        title = ""

    # Extract content with individual error handling
    try:
        if hasattr(message, 'content_as_string'):
            content = message.content_as_string()
        else:
            content = str(message.content)
    except Exception as e:
        print(f"handle_reply: content extraction failed: {e}")
        import sys as _sys
        _sys.print_exception(e)
        content = None

    if content is not None:
        print(f"\n>>> REPLY from server: {content}")
        pending_replies.append(content)


# Queue of pending replies for the main loop to drain
pending_replies = []


# ---- Main ----

def main():
    global pending_replies

    status_lines = []

    # Init display
    tft = init_display()
    tft = log("LMAO Cardputer Client — Booting...", tft, status_lines)

    # Check µReticulum availability
    if RNS is None:
        log("ERROR: µReticulum not installed!", tft, status_lines)
        log("Install ureticulum + ulxmf on your Cardputer.", tft, status_lines)
        while True:
            time.sleep(1)

    # Initialize Reticulum
    tft = log("Init Reticulum...", tft, status_lines)
    reticulum = RNS.Reticulum(config=config.config)
    tft = log("Reticulum OK.", tft, status_lines)

    # Create ephemeral identity (new each boot for POC)
    identity = RNS.Identity()
    identity_hex = RNS.hexrep(identity.hash, delimit=False)
    tft = log(f"ID: {identity_hex[:16]}...", tft, status_lines)

    # Start LXMF router
    router = LXMF.LXMRouter(identity=identity, storagepath="/flash/lxmf_state")
    router.register_delivery_callback(handle_reply)
    tft = log("LXMF router started.", tft, status_lines)

    # ---- Discovery: announce ourselves so server can find us ----
    tft = log("Announcing presence...", tft, status_lines)
    router.announce()
    tft = log("POC Ready.", tft, status_lines)

    # Display "LMAO POC Ready" prominently
    if tft is not None:
        tft = display_status(tft, ["LMAO POC Ready", f"ID: {identity_hex[:24]}"])

    # ---- Main loop: periodic hello + listen for replies ----
    seq = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10
    SERVER_IDENTITY_HASH = None  # Set this to the server's 16-byte hash if known

    while True:
        try:
            consecutive_errors = 0  # Reset error counter on successful iteration
            seq += 1

            # Send hello message — direct if destination hash known, else broadcast
            hello_text = f"Hello from Cardputer — seq {seq}"
            content = make_poc_message(identity_hex, hello_text, timestamp=int(time.time() * 1000))

            destination = None  # Broadcast by default
            if SERVER_IDENTITY_HASH is not None:
                destination = RNS.Identity.recall(SERVER_IDENTITY_HASH)

            msg = LXMF.LXMessage(
                destination=destination,
                source=identity,
                content=content,
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            router.handle_outbound(msg)
            log_prefix = "Sent broadcast" if destination is None else "Sent"
            tft = log(f"{log_prefix}: {hello_text}", tft, status_lines)

            # Drain all pending replies from the callback
            for reply in pending_replies:
                tft = log(f"Reply: {reply}", tft, status_lines)
            pending_replies.clear()

            # Wait before next cycle (10 seconds for demo)
            time.sleep(10)

        except KeyboardInterrupt:
            log("Shutting down...", tft, status_lines)
            break
        except Exception as e:
            consecutive_errors += 1
            import sys as _sys
            _sys.print_exception(e)
            tft = log(f"❗ Error ({consecutive_errors}): {e}", tft, status_lines)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log("FATAL: Too many consecutive errors, halting.", tft, status_lines)
                break
            time.sleep(5)


# Auto-run when flashed to Cardputer
if __name__ == "__main__":
    main()
