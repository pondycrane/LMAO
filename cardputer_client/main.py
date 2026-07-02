"""
LMAO Cardputer Client — µReticulum LoRa sender/receiver.

Runs on M5Stack Cardputer ADV with onboard LoRa antenna.
Uses the urns µReticulum port for MicroPython.

Sends periodic "Hello" messages and displays received replies.
"""

import time
import sys

# µReticulum imports (urns MicroPython port)
import gc
gc.collect()

# Try to import the urns library from /lib (where the flash tool places it)
try:
    sys.path.insert(0, "/lib")
    from urns import Reticulum, Identity
    from urns.lxmf import LXMRouter, LXMessage
    from urns.log import LOG_NOTICE, LOG_INFO
    HAS_URNS = True
except ImportError:
    HAS_URNS = False

# Display support (if available)
try:
    from machine import Pin, SPI
    import st7789
    HAS_DISPLAY = True
except ImportError:
    HAS_DISPLAY = False

# Proto encoder (optional — gracefully degrades if not on device)
try:
    from proto.lma_encoder import make_poc_message
    HAS_PROTO = True
except ImportError:
    HAS_PROTO = False


# ---- Display helpers ----

def init_display():
    """Initialize the Cardputer ST7789 display."""
    if not HAS_DISPLAY:
        return None
    try:
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
        tft.fill(0x0000)
        return tft
    except Exception as e:
        print(f"Display init failed: {e}")
        return None


def display_status(tft, lines):
    if tft is None:
        return None
    try:
        tft.fill(0x0000)
        y = 5
        for line in lines[:10]:
            tft.text(line, 5, y, 0xFFFF)
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

def handle_reply(message):
    """Callback invoked when an LXMF reply is received.

    NOTE: This may be invoked from a separate execution context
    (uasyncio task, callback handler, etc.). Replies are buffered
    in ``pending_replies`` for the main loop to drain — do NOT
    add blocking calls or locking here.
    """
    content = ""
    try:
        content = message.content_as_string() or ""
    except Exception as e:
        print(f"handle_reply: content extraction failed: {e}")
        # sys.print_exception is MicroPython-only; CPython fallback via traceback
        try:
            sys.print_exception(e)
        except AttributeError:
            import traceback
            traceback.print_exception(type(e), e, e.__traceback__)

    if content:
        print(f"\n>>> REPLY from server: {content}")
        pending_replies.append(content)


pending_replies = []


# ---- Helpers ----

def _needs_wifi(config):
    """Check if any UDP or TCP interface is enabled in config."""
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface", "TCPClientInterface",
        ):
            return True
    return False


def _connect_wifi(ssid, password, debug=0, timeout=15):
    """Connect to WiFi and return the assigned IP."""
    import network

    # Deactivate AP interface
    ap = network.WLAN(network.AP_IF)
    if ap.active():
        ap.active(False)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        if debug >= 1:
            print("Connecting to WiFi:", ssid)
        wlan.connect(ssid, password)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.5)

    # Disable WiFi power management for reliable UDP broadcast
    wlan.config(pm=0)

    ip = wlan.ifconfig()[0]
    if debug >= 1:
        print("Connected! IP:", ip)

    try:
        import ntptime
        ntptime.settime()
        if debug >= 1:
            print("NTP synced")
    except Exception:
        print("NTP sync failed")

    return ip


# ---- Main ----

def main():
    global pending_replies

    status_lines = []
    tft = init_display()
    tft = log("LMAO Cardputer — Booting...", tft, status_lines)

    if not HAS_URNS:
        log("ERROR: µReticulum (urns) not installed!", tft, status_lines)
        log("Run: bazel run //cardputer_client:flash", tft, status_lines)
        while True:
            time.sleep(1)

    # ---- Load config (must be on device as /config.py) ----
    try:
        from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG
    except ImportError:
        log("ERROR: Cannot import config — is config.py on device?", tft, status_lines)
        while True:
            time.sleep(1)

    gc.collect()

    # ---- Connect WiFi ----
    if _needs_wifi(CONFIG):
        tft = log("Connecting WiFi...", tft, status_lines)
        try:
            _connect_wifi(WIFI_SSID, WIFI_PASS, DEBUG)
            tft = log("WiFi OK.", tft, status_lines)
        except Exception as e:
            log(f"WiFi failed: {e} — continuing without", tft, status_lines)

    gc.collect()

    # ---- Start µReticulum ----
    tft = log("Init Reticulum...", tft, status_lines)
    try:
        rns = Reticulum(loglevel=3)
        rns.config = CONFIG
        rns.setup_interfaces()
        tft = log("Reticulum OK.", tft, status_lines)
    except Exception as e:
        log(f"FATAL: Reticulum init failed: {e}", tft, status_lines)
        while True:
            time.sleep(1)

    identity_hex = rns.identity.hexhash
    tft = log(f"ID: {identity_hex[:16]}...", tft, status_lines)

    # ---- Start LXMF router ----
    tft = log("Starting LXMF router...", tft, status_lines)
    try:
        router = LXMRouter(identity=rns.identity, storagepath="/flash/lxmf_state")
        dest = router.register_delivery_identity(rns.identity, display_name=NODE_NAME)
        router.register_delivery_callback(handle_reply)
        tft = log("LXMF router OK.", tft, status_lines)
    except Exception as e:
        log(f"FATAL: LXMF router failed: {e}", tft, status_lines)
        while True:
            time.sleep(1)

    # ---- Announce ----
    tft = log("Announcing presence...", tft, status_lines)
    try:
        router.announce()
        tft = log("POC Ready.", tft, status_lines)
    except Exception as e:
        log(f"Announce failed: {e}", tft, status_lines)

    # Display banner
    if tft is not None:
        tft = display_status(tft, ["LMAO POC Ready", f"ID: {identity_hex[:24]}"])

    # ---- Main loop: periodic hello + listen for replies ----
    seq = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10
    DEST_HASH = None  # Set to server's 16-byte hash if known

    while True:
        try:
            consecutive_errors = 0
            seq += 1
            hello_text = f"Hello from Cardputer — seq {seq}"

            if not HAS_PROTO:
                log("Proto encoder not available — cannot send", tft, status_lines)
            elif DEST_HASH is None:
                log("No destination configured — not sending", tft, status_lines)
            else:
                content = make_poc_message(identity_hex, hello_text,
                                           timestamp=int(time.time() * 1000))
                # Send via urns LXMF router
                msg = router.send_message(
                    destination_hash=DEST_HASH,
                    content=content,
                    title="p:Envelope",
                )
                if msg:
                    log(f"Sent: {hello_text}", tft, status_lines)
                else:
                    log("Send returned None", tft, status_lines)

            # Drain pending replies
            for reply in pending_replies:
                tft = log(f"Reply: {reply}", tft, status_lines)
            pending_replies.clear()

            time.sleep(10)

        except KeyboardInterrupt:
            log("Shutting down...", tft, status_lines)
            break
        except Exception as e:
            consecutive_errors += 1
            # sys.print_exception is MicroPython-only; CPython fallback
            try:
                sys.print_exception(e)
            except AttributeError:
                import traceback
                traceback.print_exception(type(e), e, e.__traceback__)
            tft = log(f"❗ Error ({consecutive_errors}): {e}", tft, status_lines)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log("FATAL: Too many consecutive errors, halting.", tft, status_lines)
                break
            time.sleep(5)


# Auto-run when flashed to Cardputer
if __name__ == "__main__":
    main()
