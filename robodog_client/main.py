"""
LMAO RoboDog client — family-calendar display (Waveshare ESP32-S3 RLCD-4.2).

Boots WiFi station mode, starts µReticulum over a single RNS **TCP client**
interface to the LMAO server (LAN :4246), registers the LXMF delivery identity,
prints the delivery hash for the operator to allow-list, then runs a duty
cycle: once at boot and every SYNC_INTERVAL_SECONDS it fetches the family
calendar with an LXMF Request (Request.kind.calendar), decodes the server's
CalendarBundle reply, renders it to the ST7305 300x400 reflective LCD via
``ui``, and persists watermark+events to an optional microSD cache.

Everything is fault-tolerant: no WiFi -> boot shows the SD cache marked
"OFFLINE"; no DEST_HASH -> announce-only (the hash line still prints); decode
errors -> logged, screen keeps the last frame; no display -> serial log only.

boot.py on the device calls ``from main import main; main()``.
"""

import gc
import sys
import time

gc.collect()

# µReticulum imports (urns MicroPython port) — same dance as the Cardputer.
_LIB_PATHS = ["/lib", "/flash/lib"]
for _lp in _LIB_PATHS:
    if _lp not in sys.path:
        sys.path.insert(0, _lp)
    try:
        from urns import Identity, Reticulum  # noqa: F401
        from urns.lxmf import LXMessage, LXMFRouter  # noqa: F401

        HAS_URNS = True
        break
    except ImportError:
        continue
else:
    HAS_URNS = False

# Shared protobuf encoder (lives in the staged proto/ dir, see cardputer_client).
try:
    from proto.lma_encoder import (
        decode_envelope,
        encode_calendar_request,
        encode_request_envelope,
    )

    HAS_PROTO = True
except ImportError:
    HAS_PROTO = False
    decode_envelope = None  # type: ignore[assignment]
    encode_calendar_request = None  # type: ignore[assignment]
    encode_request_envelope = None  # type: ignore[assignment]

# Calendar renderer (robodog_client/ui.py — pure, no hardware imports).
try:
    import ui

    HAS_UI = True
except ImportError:
    HAS_UI = False
    ui = None  # type: ignore[assignment]

# ---- Module-level defaults (overridden from config.py at boot) ------------
# Mirrors cardputer_client/main.py: helpers read these globals, so the module
# stays importable even when config.py is absent (e.g. host tooling).
DEST_HASH = None
INTERVAL = 1800
REPLY_TIMEOUT = 30
LIGHT_SLEEP = False
CALENDAR_ID = "family"
WIFI_SSID = None
WIFI_PASS = None
DEBUG = 1
NODE_NAME = "LMAO_RoboDog"
RNS_CONFIG_PATH = "/rns/config.json"
LXMF_STORAGE = "/rns/lxmf_state"
SD_CS = None
SD_SCK = 12
SD_MOSI = 11
SD_MISO = 13
SD_MOUNT = "/sd"
SD_DATA_FILE = "/sd/robodog_calendar.json"
CONFIG = {"loglevel": 3, "enable_transport": False, "interfaces": []}

# Runtime state (set by main() / handle_reply).
_ROUTER = None          # LXMRouter, so handle_reply can react if needed
_SD_OK = False          # whether the microSD card is mounted
_SEQ = 0                # request sequence counter (echoed back by the reply)
_WATERMARK = 0          # last server watermark_ms (incremental fetch cursor)
_EVENTS = []            # last good CalendarBundle events list (for caching/offline)
_LAST_BUF = None        # last rendered framebuffer (blit again via show(None))
_CAL_RESULT = None      # deferral: decode_envelope stores fresh bundles here


# ── RNS / LXMF init (mirrors cardputer_client/main.py) ────────────────────


def _ensure_dir(path):
    if not path:
        return
    try:
        import os as _os

        if not _os.path.isdir(path):
            _os.mkdir(path)
    except Exception as e:
        print("Could not ensure dir %s: %s" % (path, e))


def _convert_dest_hash(hex_val):
    """Convert a DEST_HASH hex string to bytes for the urns LXMF router."""
    if hex_val is None:
        return None
    if isinstance(hex_val, bytes):
        return hex_val
    if not isinstance(hex_val, str):
        raise ValueError(
            "DEST_HASH must be a hex string, bytes, or None, got %s" % type(hex_val).__name__
        )
    try:
        import ubinascii

        unhex = ubinascii.unhexlify
    except ImportError:
        import binascii

        unhex = binascii.unhexlify
    return unhex(hex_val)


def _init_rns(config, debug=1):
    loglevel = config.get("loglevel", 3 if debug >= 2 else (2 if debug else 1))
    rns = Reticulum(loglevel=loglevel, config_path=RNS_CONFIG_PATH)
    rns.config = config
    rns.setup_interfaces()
    return rns


def _init_lxmf_router(identity, storage_path=LXMF_STORAGE, display_name=""):
    router = LXMFRouter(identity=identity, storagepath=storage_path)
    router.register_delivery_identity(identity, display_name=display_name)
    router.register_delivery_callback(handle_reply)
    return router


def _connect_wifi(ssid, password, debug=0, timeout=15):
    """Connect WiFi station mode and sync the RTC via NTP.  Returns the IP."""
    import network

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

    try:
        wlan.config(pm=0)  # disable power save for reliable TCP keepalive
    except Exception:
        pass

    ip = wlan.ifconfig()[0]
    if debug >= 1:
        print("Connected! IP:", ip)

    try:
        import ntptime

        ntptime.settime()
        if debug >= 1:
            print("NTP synced")
    except Exception as e:
        print("NTP sync failed: %s" % e)

    return ip


# ── Display ────────────────────────────────────────────────────────────────


def _init_display():
    """Init the ST7305 (lazy import — absent on host/test envs)."""
    try:
        import st7305

        display = st7305.ST7305()
        display.init()
        print("Display ready (ST7305 %dx%d)" % (st7305.ST7305.WIDTH, st7305.ST7305.HEIGHT))
        return display
    except Exception as e:
        print("Display init failed: %s (serial-only mode)" % e)
        return None


def _header_now():
    """Best-effort "YYYY-MM-DD  HH:MM" header from the local clock."""
    try:
        lt = time.localtime()
        return "%04d-%02d-%02d  %02d:%02d" % (lt[0], lt[1], lt[2], lt[3], lt[4])
    except Exception:
        return None


def _render(display, events, now_ms=None, offline=False):
    """Render *events* to the ST7305 via ui; returns True on success."""
    global _LAST_BUF
    if display is None or ui is None:
        return False
    try:
        _LAST_BUF = ui.render(
            display, events, now_ms=now_ms, header_text=None, offline=offline
        )
        print(
            "rendered %d events%s"
            % (len(events or []), " (offline cached)" if offline else "")
        )
        return True
    except Exception as e:
        sys.print_exception(e)
        print("render failed: %s" % e)
        return False


def _render_offline_cached(display):
    """Show the cached calendar marked OFFLINE; else keep the last frame."""
    global _EVENTS, _LAST_BUF
    if display is None or ui is None:
        return
    try:
        if _EVENTS:
            _render(display, _EVENTS, offline=True)
        elif _LAST_BUF is not None:
            display.show(None)  # keep the last good image
        else:
            ui.render(display, [], offline=True)  # header-only "OFFLINE ..."
    except Exception as e:
        print("offline render failed: %s" % e)


# ── Optional microSD cache ─────────────────────────────────────────────   ─


def _sd_mount():
    """Mount the microSD card if configured + possible.  Fully fault-tolerant.

    Tries the firmware's machine.SDCard first, then the common ``sdcard.py``
    module.  Any failure disables caching silently (SD is optional).
    """
    global SD_CS
    if SD_CS is None:
        return False
    try:
        import os as uos
    except Exception:
        return False

    try:
        from machine import Pin, SDCard  # some ESP32 builds expose machine.SDCard

        try:
            sd = SDCard(slot=1)
        except Exception:
            sd = SDCard(sd_pins=(SD_SCK, SD_MOSI, SD_MISO, SD_CS))
        uos.mount(sd, SD_MOUNT)
        print("SD mounted via machine.SDCard")
        return True
    except Exception:
        pass

    try:
        from machine import Pin, SPI
        from sdcard import SDCard  # classic micropython-sdcard driver module

        spi = SPI(1, baudrate=4000000, polarity=0, phase=0,
                  sck=Pin(SD_SCK), mosi=Pin(SD_MOSI), miso=Pin(SD_MISO))
        sd = SDCard(spi, Pin(SD_CS))
        uos.mount(sd, SD_MOUNT)
        print("SD mounted via sdcard module")
        return True
    except Exception as e:
        print("SD unavailable: %s" % e)
        return False


def _sd_save(events, watermark):
    if not _SD_OK:
        return
    try:
        import json

        with open(SD_DATA_FILE, "w") as f:
            json.dump({"watermark_ms": watermark, "events": events or []}, f)
    except Exception as e:
        print("SD save failed: %s" % e)


def _sd_load():
    if not _SD_OK:
        return [], 0
    try:
        import json

        with open(SD_DATA_FILE) as f:
            data = json.load(f)
        return list(data.get("events") or []), int(data.get("watermark_ms") or 0)
    except Exception:
        return [], 0


def _lightsleep_ms(ms):
    try:
        from machine import lightsleep

        lightsleep(ms)
    except Exception:
        pass


# ── LXMF reply handler ──────────────────────────────────────────────────────


def handle_reply(message):
    """Delivery callback: decode the LMAOEnvelope; stash fresh calendar bundles.

    Called by the LXMF router when the server replies.  Stores the decoded
    CalendarBundle in ``_CAL_RESULT`` (with its envelope seq) for the sync
    loop to claim after the matching request — non-blocking, no I/O here.
    """
    global _CAL_RESULT
    raw = getattr(message, "content", b"") or b""
    if not raw:
        return
    if not HAS_PROTO or decode_envelope is None:
        return
    try:
        result = decode_envelope(bytes(raw))
    except Exception as e:
        print("handle_reply: decode failed: %s" % e)
        return
    if not isinstance(result, dict):
        return
    if result.get("payload") == "calendar":
        _CAL_RESULT = {
            "seq": result.get("seq") or 0,
            "events": list(result.get("events") or []),
            "watermark_ms": result.get("watermark_ms") or 0,
            "calendar_id": result.get("calendar_id") or CALENDAR_ID,
        }
        print(
            "CalendarBundle: %d events, watermark=%d (reply seq=%d)"
            % (len(_CAL_RESULT["events"]), _CAL_RESULT["watermark_ms"], _CAL_RESULT["seq"])
        )


# ── Duty cycle ──────────────────────────────────────────────────────────────


async def _periodic_sync(display, router, dest_hash, interval,
                         reply_timeout, use_light_sleep):
    """Fetch the family calendar: once immediately, then every *interval*.

    Builds ``encode_request_envelope(seq, "calendar",
    encode_calendar_request(CALENDAR_ID, since_ms=watermark, 0),
    request_ack=True)``, sends it to *dest_hash* over LXMF, waits (bounded by
    *reply_timeout*) for the matching CalendarBundle, renders it and persists
    watermark/events to SD.  Any failure degrades gracefully to the cached
    view; the VM always yields to the RNS event loop between cycles.
    """
    import uasyncio as asyncio

    global _SEQ, _WATERMARK, _EVENTS, _CAL_RESULT
    while True:
        try:
            gc.collect()

            if ui is None or not HAS_PROTO:
                print("ui/proto unavailable — cannot fetch/render calendar")
            elif dest_hash is None:
                print(
                    "No DEST_HASH on device — announce-only; operator: add "
                    "`RoboDog lxmf/delivery` to server ALLOWED_CLIENTS"
                )
                _render_offline_cached(display)
            else:
                _SEQ += 1
                seq = _SEQ
                try:
                    req_bytes = encode_calendar_request(
                        calendar_id=CALENDAR_ID, since_ms=_WATERMARK, max_events=0
                    )
                    envelope = encode_request_envelope(
                        seq, "calendar", req_bytes, request_ack=True
                    )
                except Exception as e:
                    sys.print_exception(e)
                    print("calendar request encode failed: %s" % e)
                    _render_offline_cached(display)
                else:
                    try:
                        router.send_message(
                            destination_hash=dest_hash,
                            content=envelope,
                            title="p:Envelope",
                            desired_method=LXMessage.OPPORTUNISTIC,
                        )
                        print("calendar request sent seq=%d since_ms=%d" % (seq, _WATERMARK))
                    except Exception as e:
                        sys.print_exception(e)
                        print("calendar request send failed: %s" % e)
                        _render_offline_cached(display)
                    else:
                        # Bounded wait for the reply with a matching envelope seq.
                        _CAL_RESULT = None
                        got = False
                        deadline = time.time() + reply_timeout
                        while time.time() < deadline:
                            await asyncio.sleep(1)
                            if _CAL_RESULT and _CAL_RESULT.get("seq") == seq:
                                got = True
                                break

                        if got:
                            bundle = _CAL_RESULT
                            _CAL_RESULT = None
                            _EVENTS = bundle["events"]
                            _WATERMARK = bundle.get("watermark_ms") or _WATERMARK
                            _render(display, _EVENTS, now_ms=int(time.time() * 1000))
                            _sd_save(_EVENTS, _WATERMARK)
                            print("calendar OK: %d events, watermark=%d"
                                  % (len(_EVENTS), _WATERMARK))
                        else:
                            print("calendar reply timeout (seq=%d)" % seq)
                            _render_offline_cached(display)

        except Exception as e:
            # Never let one bad cycle kill the loop.
            sys.print_exception(e)
            print("sync cycle error: %s" % e)
            _render_offline_cached(display)

        # ---- idle between cycles ----
        if use_light_sleep:
            # Battery duty cycle: suspend the VM (RX resumes after wake; the
            # server only pushes calendars in response to our requests anyway).
            _lightsleep_ms(int(interval * 1000))
        else:
            await asyncio.sleep(interval)


async def _async_runtime(rns, router, display, dest_hash, interval,
                         reply_timeout, use_light_sleep):
    """Run the urns event loop (job_loop + poll_loop) plus the sync task."""
    import uasyncio as asyncio

    asyncio.create_task(
        _periodic_sync(display, router, dest_hash, interval, reply_timeout, use_light_sleep)
    )
    await rns.run()


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    global DEST_HASH, INTERVAL, REPLY_TIMEOUT, LIGHT_SLEEP, CALENDAR_ID
    global WIFI_SSID, WIFI_PASS, DEBUG, NODE_NAME, RNS_CONFIG_PATH, LXMF_STORAGE
    global SD_CS, SD_SCK, SD_MOSI, SD_MISO, SD_MOUNT, SD_DATA_FILE, CONFIG
    global _ROUTER, _SD_OK, _WATERMARK, _EVENTS

    gc.collect()
    print("RoboDog calendar display — booting...")

    display = _init_display()

    if not HAS_URNS:
        print("ERROR: µReticulum (urns) not installed!")
        while True:
            time.sleep(1)

    # ---- Load config (must be on device as /config.py) ----
    try:
        from config import (  # noqa: F401
            CALENDAR_ID,
            CONFIG,
            DEBUG,
            DEST_HASH as _RAW_DEST,
            LIGHT_SLEEP,
            LXMF_STORAGE,
            NODE_NAME,
            REPLY_TIMEOUT_SECONDS,
            RNS_CONFIG_PATH,
            SD_CS,
            SD_DATA_FILE,
            SD_MISO,
            SD_MOUNT,
            SD_MOSI,
            SD_SCK,
            SYNC_INTERVAL_SECONDS,
            WIFI_PASS,
            WIFI_SSID,
        )
    except (ImportError, SyntaxError, ValueError) as e:
        print("ERROR: cannot load config.py (%s) — is it on device?" % e)
        while True:
            time.sleep(1)

    DEST_HASH = _convert_dest_hash(_RAW_DEST)
    INTERVAL = max(int(SYNC_INTERVAL_SECONDS), 5)
    REPLY_TIMEOUT = max(int(REPLY_TIMEOUT_SECONDS), 5)
    print("Cfg: sync=%ds reply_timeout=%ds dest_hash=%s"
          % (INTERVAL, REPLY_TIMEOUT, "set" if DEST_HASH else "None"))

    gc.collect()

    # ---- WiFi ----
    if WIFI_SSID:
        print("Connecting WiFi: %s" % WIFI_SSID)
        try:
            _connect_wifi(WIFI_SSID, WIFI_PASS, DEBUG)
            print("WiFi OK.")
        except Exception as e:
            print("WiFi failed: %s — continuing (cached/offline)" % e)
    else:
        print("No WIFI_SSID configured — continuing (cached/offline)")

    gc.collect()

    # ---- Optional SD cache ----
    _SD_OK = _sd_mount()
    _EVENTS, _WATERMARK = _sd_load()
    if _EVENTS:
        print("SD cache loaded: %d events, watermark=%d" % (len(_EVENTS), _WATERMARK))

    # ---- Start µReticulum ----
    _ensure_dir(RNS_CONFIG_PATH.rsplit("/", 1)[0] if "/" in RNS_CONFIG_PATH else ".")
    _ensure_dir(LXMF_STORAGE)
    try:
        rns = _init_rns(CONFIG, DEBUG)
        print("Reticulum OK.")
    except Exception as e:
        print("FATAL: Reticulum init failed: %s" % e)
        while True:
            time.sleep(1)

    identity_hex = rns.identity.hexhash
    print("Identity: %s..." % identity_hex[:16])

    # ---- LXMF router + delivery identity ----
    try:
        router = _init_lxmf_router(rns.identity, storage_path=LXMF_STORAGE, display_name=NODE_NAME)
        print("LXMF router OK.")
    except Exception as e:
        print("FATAL: LXMF router failed: %s" % e)
        while True:
            time.sleep(1)

    _ROUTER = router

    # Operator action: the server only accepts messages whose sender
    # lxmf/delivery hash is in ALLOWED_CLIENTS (LMAO_ALLOWED_CLIENTS in
    # k8s/lmao-server.yaml) — print ours once so it can be added.
    try:
        delivery = router.delivery_destination
        hexhash = delivery.hexhash if delivery is not None else ""
        print("RoboDog lxmf/delivery " + hexhash)
        print("  add this hash to the server ALLOWED_CLIENTS to let it fetch calendars")
    except Exception as e:
        print("Could not derive delivery hash: %s" % e)

    # ---- Announce presence so the server learns our identity/path ----
    try:
        router.announce()
        print("Announced.")
    except Exception as e:
        print("Announce failed: %s" % e)

    # ---- Initial render (cached or empty-offline) so the panel is never black
    if _EVENTS:
        _render_offline_cached(display)
    elif display is not None and ui is not None:
        try:
            ui.render(display, [], offline=True)
        except Exception as e:
            print("initial render failed: %s" % e)

    # ---- Run the duty cycle alongside the Reticulum event loop ----
    print("Starting event loop (fetch every %ds)..." % INTERVAL)
    try:
        import uasyncio as asyncio

        asyncio.run(
            _async_runtime(rns, router, display, DEST_HASH, INTERVAL, REPLY_TIMEOUT, LIGHT_SLEEP)
        )
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        sys.print_exception(e)

    print("Halting.")


# Auto-run when flashed to the RLCD board.
if __name__ == "__main__":
    main()
