"""
LMAO Cardputer Client — µReticulum LoRa sender/receiver.

Runs on M5Stack Cardputer ADV with onboard LoRa antenna.
Uses the urns µReticulum port for MicroPython.

Sends periodic "Hello" messages and displays received replies.
"""

# µReticulum imports (urns MicroPython port)
import gc
import sys
import time

gc.collect()

# Try to import the urns library from /lib (where the flash tool places it)
try:
    sys.path.insert(0, "/lib")
    from urns import Identity, Reticulum  # noqa: F401
    from urns.log import LOG_INFO, LOG_NOTICE  # noqa: F401
    from urns.lxmf import LXMessage, LXMRouter  # noqa: F401

    HAS_URNS = True
except ImportError:
    HAS_URNS = False

# Display support (if available)
try:
    import st7789
    from machine import SPI, Pin

    HAS_DISPLAY = True
except ImportError:
    HAS_DISPLAY = False

# Proto encoder (optional — gracefully degrades if not on device)
try:
    from proto.lma_encoder import encode_sensor_envelope, make_poc_message

    HAS_PROTO = True
except ImportError:
    HAS_PROTO = False

# Feature flag to disable SensorReport sending; defaults True for backward compatibility
SEND_SENSOR = True

# Default interval and sensor settings; updated from config.py at boot time.
# Module-level defaults allow make_sensor_message() to function even when
# no config.py is present (e.g., in test environments).
_CONFIG = {
    "interval_seconds": 60,
    "sensor_type": None,
    "sensor_i2c_addr": 0x38,
}

# ---- Sensor library detection ----
try:
    from lib.sensors import read_humidity_temperature

    HAS_SENSOR_LIB = True
except ImportError:
    HAS_SENSOR_LIB = False
    read_humidity_temperature = None  # type: ignore[assignment]


def _min_interval(val):
    """Clamp the send interval to at least 10 seconds to avoid LoRa congestion."""
    return max(val, 10)


def _init_rns(config):
    """Initialize µReticulum with the given config dict.

    Returns the Reticulum instance.  Raises on failure — the
    caller (``main()``) is responsible for error handling.
    """
    rns = Reticulum(loglevel=3)
    rns.config = config
    rns.setup_interfaces()
    return rns


def _init_lxmf_router(identity, storage_path="/flash/lxmf_state", display_name=""):
    """Create and configure an LXMF router bound to *identity*.

    Registers the delivery identity, attaches the reply callback,
    and returns the router.  Raises on failure — the caller
    (``main()``) is responsible for error handling.
    """
    router = LXMRouter(identity=identity, storagepath=storage_path)
    router.register_delivery_identity(identity, display_name=display_name)
    router.register_delivery_callback(handle_reply)
    return router


def _init_wifi(ssid, password, config, debug=0):
    """Connect to WiFi if the config requires it.

    Returns ``True`` if WiFi was attempted, ``False`` otherwise.
    Failures are logged but do NOT halt the boot sequence.
    """
    if not _needs_wifi(config):
        return False
    _connect_wifi(ssid, password, debug)
    return True


def make_sensor_message(identity_hex, seq, battery=3.7, strict=False):
    """Build an LMAOEnvelope containing a SensorReport with real ESP32 die temperature.

    When *strict* is ``False`` (default): on CPython (no ``esp32`` module) falls back
    to a constant 25.0°C for test environments.  On real hardware, sensor read
    failures propagate as exceptions regardless of the flag.

    When *strict* is ``True``: raises ``RuntimeError`` if the ESP32 die temperature
    cannot be read, even on CPython.  Use this in E2E tests or production configs
    that must never see synthetic data.

    Args:
        identity_hex: Hex identity string of the sending node.
        seq: Sequence number.
        battery: Battery voltage (default 3.7 V).
        strict: If ``True``, fail hard when temperature can't be read from hardware.

    Returns:
        bytes: Serialized LMAOEnvelope protobuf.
    """

    try:
        import esp32

    except ImportError:
        if strict:
            raise RuntimeError(
                "esp32 module not available and strict=True — cannot read "
                "CPU temperature without a real ESP32 device"
            )
        # Fallback for non-ESP32 environments; preserves backward compatibility
        temp = 25.0

    else:
        temp = (esp32.raw_temperature() - 32) * 5.0 / 9.0  # Fahrenheit to Celsius

    readings = [
        {
            "sensor_id": 1,
            "value": temp,
            "unit": "C",
            "timestamp_ms": int(time.time() * 1000),
        }
    ]

    # ---- Humidity sensor (external Grove I2C) ----
    if _CONFIG["sensor_type"] is not None and HAS_SENSOR_LIB:
        try:
            _, humidity = read_humidity_temperature(
                _CONFIG["sensor_type"], _CONFIG["sensor_i2c_addr"]
            )
            if humidity is not None:
                readings.append(
                    {
                        "sensor_id": 2,
                        "value": humidity,
                        "unit": "%",
                        "timestamp_ms": int(time.time() * 1000),
                    }
                )
        except (OSError, ValueError) as e:
            if hasattr(sys, "print_exception"):
                sys.print_exception(e)
            print(f"Humidity sensor read failed: {e}")

    return encode_sensor_envelope(identity_hex, seq, battery, readings)


# ---- Display helpers ----


def init_display():
    """Initialize the Cardputer ST7789 display."""
    if not HAS_DISPLAY:
        return None
    try:
        spi = SPI(
            1,
            baudrate=40000000,
            polarity=1,
            phase=0,
            sck=Pin(36),
            mosi=Pin(35),
            miso=Pin(37),
        )
        tft = st7789.ST7789(
            spi,
            240,
            135,
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
        sys.print_exception(e)

    if content:
        print(f"\n>>> REPLY from server: {content}")
        pending_replies.append(content)


pending_replies: list[str] = []


# ---- Helpers ----


def _needs_wifi(config):
    """Check if any UDP or TCP interface is enabled in config."""
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface",
            "TCPClientInterface",
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


# ---- Config helpers ----


def _convert_dest_hash(hex_val):
    """Convert a DEST_HASH hex string to bytes for the urns LXMF router.

    Args:
        hex_val: A hex string (e.g. "a1b2c3d4e5f6..."), bytes, or None.

    Returns:
        bytes: The decoded byte sequence, or the original bytes passed
               through unchanged, or None if None was passed.

    Raises:
        ValueError: hex_val is a string but not valid hex, or an
                    unsupported type.
    """
    if hex_val is None:
        return None
    if isinstance(hex_val, bytes):
        return hex_val
    if not isinstance(hex_val, str):
        raise ValueError(
            f"DEST_HASH must be a hex string, bytes, or None, got {type(hex_val).__name__}"
        )
    try:
        import ubinascii

        unhex = ubinascii.unhexlify
    except ImportError:
        import binascii

        unhex = binascii.unhexlify
    return unhex(hex_val)


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
        from config import CONFIG, DEBUG, DEST_HASH, NODE_NAME, WIFI_PASS, WIFI_SSID

        raw_dest = DEST_HASH  # captured for error messages
        DEST_HASH = _convert_dest_hash(DEST_HASH)

        # Optional new config constants — gracefully handle missing values
        try:
            from config import INTERVAL_SECONDS, SENSOR_I2C_ADDR, SENSOR_TYPE
        except ImportError:
            INTERVAL_SECONDS = 60
            SENSOR_TYPE = None
            SENSOR_I2C_ADDR = 0x38

        # ---- Apply config values ----
        # Clamp interval to minimum 10s to avoid LoRa congestion
        INTERVAL_SECONDS = _min_interval(INTERVAL_SECONDS)

        # Sync sensor config to _CONFIG dict (needed by make_sensor_message)
        _CONFIG["sensor_type"] = SENSOR_TYPE
        _CONFIG["sensor_i2c_addr"] = SENSOR_I2C_ADDR
        _CONFIG["interval_seconds"] = INTERVAL_SECONDS

    except ValueError:
        log(
            f"ERROR: DEST_HASH is not a valid hex string: {raw_dest!r}. "
            "Expected 32 hex characters (e.g. 'a1b2c3d4e5f6...'). "
            "Set DEST_HASH = None in config.py to disable sending.",
            tft,
            status_lines,
        )
        while True:
            time.sleep(1)

    except ImportError:
        log("ERROR: Cannot import config — is config.py on device?", tft, status_lines)
        while True:
            time.sleep(1)

    gc.collect()

    # ---- Connect WiFi ----
    if _needs_wifi(CONFIG):
        tft = log("Connecting WiFi...", tft, status_lines)
        try:
            _init_wifi(WIFI_SSID, WIFI_PASS, CONFIG, DEBUG)
            tft = log("WiFi OK.", tft, status_lines)
        except Exception as e:
            log(f"WiFi failed: {e} — continuing without", tft, status_lines)

    gc.collect()

    # ---- Start µReticulum ----
    tft = log("Init Reticulum...", tft, status_lines)
    try:
        rns = _init_rns(CONFIG)
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
        router = _init_lxmf_router(rns.identity, display_name=NODE_NAME)
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
                content = make_poc_message(
                    identity_hex, hello_text, timestamp=int(time.time() * 1000)
                )
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

                # Also send SensorReport if enabled (dual-send alongside TextMessage)
                if SEND_SENSOR:
                    try:
                        sensor_content = make_sensor_message(identity_hex, seq)
                        msg2 = router.send_message(
                            destination_hash=DEST_HASH,
                            content=sensor_content,
                            title="p:Envelope",
                        )
                        if msg2:
                            log(f"Sensor: seq={seq}", tft, status_lines)
                        else:
                            log("Sensor send returned None", tft, status_lines)
                    except Exception as sensor_err:
                        sys.print_exception(sensor_err)
                        log(f"Sensor send failed: {sensor_err}", tft, status_lines)

            # Drain pending replies
            for reply in pending_replies:
                tft = log(f"Reply: {reply}", tft, status_lines)
            pending_replies.clear()

            time.sleep(_CONFIG["interval_seconds"])

        except KeyboardInterrupt:
            log("Shutting down...", tft, status_lines)
            break
        except Exception as e:
            consecutive_errors += 1
            sys.print_exception(e)
            tft = log(
                f"❗ Error in main loop "
                f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): "
                f"{type(e).__name__}: {e}",
                tft,
                status_lines,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log("FATAL: Too many consecutive errors, halting.", tft, status_lines)
                break
            time.sleep(5)


# Auto-run when flashed to Cardputer
if __name__ == "__main__":
    main()
