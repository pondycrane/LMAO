"""
Cardputer Flash Tool — Upload MicroPython client code to M5Stack Cardputer.

Uses serial communication (pyserial) to transfer cardputer_client/*.py files
to a connected Cardputer running MicroPython via raw REPL mode.

Usage (via Bazel):
    bazel run //cardputer_client:flash
    bazel run //cardputer_client:flash -- --port /dev/ttyACM0
    bazel run //cardputer_client:flash -- --verify-only

Prerequisites:
    - Cardputer with MicroPython installed, connected via USB
    - User has permissions on the serial port (dialout group)
"""

import argparse
import base64
import os
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Error: pyserial is required. Install with: pip install pyserial")
    sys.exit(1)

# ---- File discovery ----

# MicroPython source files to upload (relative to cardputer_client/).
# All files are uploaded to the device root (/), preserving the relative
# directory structure (e.g., proto/lma_encoder.py → /proto/lma_encoder.py).
#
# Library files (urns/ and native .mpy modules) are uploaded to /lib/.
FILES_TO_UPLOAD = [
    "boot.py",
    "config.py",
    "lora_boards.py",
    "main.py",
    "proto/lma_encoder.py",
]


# Library files to upload under /lib/ (µReticulum urns port + native modules).
# These are the .py files from the urns package and .mpy native crypto modules.
#
# Auto-discovered via os.walk() — any new .py or .mpy file added to lib/
# will be automatically included at the next flash.  No manual list updates
# are needed.
def auto_discover_lib_files(client_root):
    """Walk the lib/ directory and return relative paths to all .py and .mpy files.

    Returns a sorted list of paths relative to *client_root* (e.g.,
    ``lib/urns/__init__.py``) so that upload order is deterministic.
    """
    lib_dir = os.path.join(client_root, "lib")
    if not os.path.isdir(lib_dir):
        print(f"WARNING: Library directory not found: {lib_dir}")
        print("         No library files will be uploaded.")
        return []
    files = []
    for dirpath, _dirnames, filenames in os.walk(lib_dir):
        for f in filenames:
            if f.endswith((".py", ".mpy")):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, client_root)
                files.append(rel)
    files.sort()
    return files


# ---- Path helpers ----


def _upload_files(ser, client_root, file_list):
    """Upload a list of files to the device, exiting on failure.

    Files whose content already matches the device copy are skipped
    (SHA-256 compare), which makes re-flashing fast.  Aborts the whole
    run immediately when the device stalls mid-upload.
    """
    for rel in file_list:
        local_path = os.path.join(client_root, rel)
        print(f"  {rel:30s} … ", end="", flush=True)
        try:
            res = upload_file(ser, local_path, rel, skip_if_unchanged=True)
        except DeviceStalledError as e:
            print("FAILED")
            print(f"\nERROR: {e}")
            sys.exit(1)
        if res == "unchanged":
            print("OK  (unchanged)")
        elif res:
            size = os.path.getsize(local_path)
            print(f"OK  ({size} B)")
        else:
            print("FAILED")
            sys.exit(1)


def _sanitize_path_for_script(path: str) -> str:
    """Validate and escape a remote path for embedding in a MicroPython
    string literal.

    Raises *ValueError* when the path contains backslashes or non-printable
    characters that cannot be safely escaped.  Single-quotes are escaped so
    they do not break the generated MicroPython string.
    """
    if "\\" in path:
        raise ValueError(f"Path contains backslash, cannot safely embed in script: {path!r}")
    # Escape single quotes for the MicroPython string literal
    path = path.replace("'", "\\'")
    # Basic sanity: reject non-printable characters
    if not all(32 <= ord(c) < 127 for c in path):
        raise ValueError(f"Path contains non-printable characters: {path!r}")
    return path


def verify_files_exist(client_root, file_list):
    """Check that every file in *file_list* exists under *client_root*.

    Raises:
        FileNotFoundError: If any required file is missing.
    """
    for rel in file_list:
        full = os.path.join(client_root, rel)
        if not os.path.isfile(full):
            raise FileNotFoundError(f"Required file not found: {full}")


def find_client_root():
    """Locate the cardputer_client/ source directory.

    Checks (in order):
      1. BUILD_WORKSPACE_DIRECTORY (set by ``bazel run``)
      2. TEST_SRCDIR / _main (set by ``bazel test``, module workspace)
      3. Relative to current working directory
    """
    # 1. bazel run
    ws = os.environ.get("BUILD_WORKSPACE_DIRECTORY", "")
    if ws:
        candidate = os.path.join(ws, "cardputer_client")
        if os.path.isdir(candidate):
            return candidate

    # 2. bazel test — runfiles tree
    test_srcdir = os.environ.get("TEST_SRCDIR", "")
    if test_srcdir:
        for workspace_name in ("_main", "lmao"):
            candidate = os.path.join(test_srcdir, workspace_name, "cardputer_client")
            if os.path.isdir(candidate):
                return candidate

    # 3. cwd fallback
    if os.path.isdir("cardputer_client"):
        return "cardputer_client"

    return None


# ---- Serial helpers ----


def find_cardputer_port(preferred=None):
    """Return the serial device path for a connected Cardputer (ESP32-S3).

    When *preferred* is given it is returned immediately (caller-supplied port).
    Otherwise all available serial ports are scanned using the shared
    device detection library (:mod:`lma_core.device_detect`), which uses
    VID/PID + product strings for exact identification — **no** broad
    keyword fallback matching.
    """
    if preferred:
        return preferred

    try:
        from lma_core.device_detect import find_cardputer_port as _find

        return _find()
    except ImportError:
        pass

    # Fallback when lma_core is not importable (e.g. running outside Bazel)
    try:
        ports = serial.tools.list_ports.comports()
    except Exception as e:
        print(f"WARNING: Could not enumerate serial ports: {e}")
        print("Specify the port manually with --port /dev/ttyACM0")
        return None

    for p in ports:
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        if vid == 0x303A and pid == 0x8120:  # M5Stack Cardputer ADV (ESP32-S3)
            return p.device

    return None


def enter_raw_repl(ser, max_attempts=5):
    """Sends Ctrl+C (twice) + Ctrl+A to enter MicroPython raw REPL mode.

    Retries the interrupt sequence up to *max_attempts* times.  A single
    Ctrl+C window can be missed when the device is busy in a long blocking
    section (e.g. a split-frame LoRa TX burst takes ~800ms, crypto, gc),
    so the whole sequence is repeated until the ``raw REPL; CTRL-B to
    exit`` banner is received or all attempts are exhausted (~15s total).
    """
    try:
        for attempt in range(max_attempts):
            # Interrupt anything that may be running
            ser.write(b"\r\x03\x03")
            time.sleep(0.5)
            ser.read(ser.in_waiting)  # drain any residual output

            # Request raw REPL
            ser.write(b"\r\x01")
            time.sleep(0.3)

            data = b""
            deadline = time.time() + 2
            while time.time() < deadline:
                if ser.in_waiting:
                    data += ser.read(ser.in_waiting)
                if b"raw REPL" in data:
                    # Give device a moment to print the '>' prompt, then drain
                    time.sleep(0.15)
                    ser.read(ser.in_waiting)
                    return True
                time.sleep(0.05)

            if attempt == 0:
                print(
                    "Device did not respond to interrupt — retrying "
                    "(it may be busy in its main loop)..."
                )

        return False
    except (serial.SerialException, OSError) as e:
        print(f"ERROR: Serial communication lost while entering raw REPL: {e}")
        return False


def exit_raw_repl(ser):
    """Return to friendly REPL by sending Ctrl+B."""
    try:
        ser.write(b"\r\x02")
        time.sleep(0.2)
        ser.read(ser.in_waiting)
    except (serial.SerialException, OSError):
        pass  # device may already be gone; nothing to recover


def exec_raw(ser, code, timeout=15):
    """Send *code* in raw REPL mode and return (ok, output).

    Sends the code followed by Ctrl+D, then reads until the ``\\x04>``
    sequence or *timeout* expires.  Returns ``(True, output_string)`` when
    the response contains ``OK``, ``(False, reason_or_output)`` otherwise.
    """
    if isinstance(code, str):
        code = code.encode("utf-8")

    try:
        ser.write(code)
        ser.write(b"\x04")

        data = b""
        deadline = time.time() + timeout
        found_term = False
        while time.time() < deadline:
            if ser.in_waiting:
                data += ser.read(ser.in_waiting)
            if b"\x04>" in data:
                found_term = True
                break
            time.sleep(0.05)

        if not found_term:
            return False, "Timeout waiting for device response"

        output = data.decode("utf-8", errors="replace")
        ok = "OK" in output
        return ok, output
    except (serial.SerialException, OSError) as e:
        error_msg = f"Serial communication error during exec_raw: {e}"
        print(f"  ERROR: {error_msg}")
        return False, error_msg


def verify_device(ser):
    """Check that the connected device is an ESP32 running MicroPython.

    Returns ``(True, info_string)`` or ``(False, reason_string)``.
    """
    ok, out = exec_raw(
        ser,
        """
import sys as _sys
import os as _os
print(_sys.platform)
print(_os.uname().machine)
""",
    )
    if not ok:
        return False, f"exec_raw failed: {out[:200]}"

    lines = [line for line in out.split("\n") if line and line != "OK"]
    platform = lines[0] if lines else ""
    machine = lines[1] if len(lines) > 1 else ""

    if "esp32" in platform.lower() or "esp32" in machine.lower():
        return True, f"ESP32 / {platform} / {machine}"
    return False, f"Not an ESP32 device — platform={platform!r}, machine={machine!r}"


class DeviceStalledError(Exception):
    """The device stopped responding mid-upload.

    Raised when several consecutive chunk writes time out, which almost
    always means the ESP32-S3 USB-Serial-JTAG interface has wedged (it
    cannot be recovered from software — a physical reset is required).
    """


# Number of consecutive chunk-write failures before declaring the device
# wedged and aborting the upload.  Each failed attempt costs one exec_raw
# timeout (~15s), so 3 failures ≈ 45s before giving up.
_STALL_LIMIT = 3


def device_file_sha256(ser, remote_path):
    """Return the hex SHA-256 of a file on the device, or *None*.

    *remote_path* must already be a device-absolute path (i.e. with the
    ``/flash`` prefix applied) — it is NOT prefixed again here.

    Returns *None* when the file does not exist on the device or the
    digest could not be computed.  Used to skip uploading files whose
    content is already present, which makes re-installs fast.
    """
    remote_path_esc = _sanitize_path_for_script(remote_path)
    script = (
        b"import uhashlib as _h\n"
        b"try:\n"
        b"    _f = open('" + remote_path_esc.encode("utf-8") + b"', 'rb')\n"
        b"    _s = _h.sha256()\n"
        b"    while True:\n"
        b"        _b = _f.read(4096)\n"
        b"        if not _b:\n"
        b"            break\n"
        b"        _s.update(_b)\n"
        b"    _f.close()\n"
        b"    print('SHA:' + ''.join('%02x' % _c for _c in _s.digest()))\n"
        b"except OSError:\n"
        b"    print('SHA:MISSING')\n"
    )
    ok, out = exec_raw(ser, script, timeout=10)
    if not ok:
        if "Timeout" in out:
            # Device is not answering — no point grinding through the
            # remaining files at one timeout each.
            raise DeviceStalledError(
                f"Device stopped responding while checking {remote_path}. "
                "The USB-Serial-JTAG interface may be wedged — press the "
                "Cardputer's RESET button (or power-cycle it), then retry."
            )
        return None
    for line in out.splitlines():
        if line.startswith("SHA:"):
            val = line[4:].strip()
            return None if val == "MISSING" else val
    return None


def upload_file(ser, local_path, remote_path, chunk_size=1024, skip_if_unchanged=False):
    """Upload a single file to the MicroPython device using raw REPL.

    The file content is sent in multiple small ``exec_raw`` calls rather
    than one large script.  This works around MicroPython's heap memory
    limit on ESP32-S3 (~6 KB for REPL paste compilation), which would
    crash the device with ``MemoryError`` for files larger than a few KB
    if sent as a single paste.

    When *skip_if_unchanged* is True, the file's SHA-256 is compared
    against the copy already on the device and the upload is skipped
    when they match (returns the string ``"unchanged"``, which is
    truthy, instead of ``True``).

    Raises :class:`DeviceStalledError` when the device stops responding
    mid-upload (consecutive chunk timeouts) instead of grinding through
    multi-minute timeouts for every remaining chunk.

    In raw REPL mode, variables defined at module scope persist between
    consecutive paste blocks.  We exploit this to keep a single file
    handle open across multiple ``exec_raw`` calls:

      1. Create target directory (if needed)         — single exec_raw
      2. Remove any existing file                     — single exec_raw
      3. ``f = open(...)`` to open for writing        — single exec_raw
      4. ``f.write(...)`` for each base64 chunk       — one exec_raw each
      5. ``f.close()`` and print ``UPLOAD_OK``        — single exec_raw

    Parameters
    ----------
    ser : serial.Serial
        Open serial connection to the device (must be in raw REPL mode).
    local_path : str
        Path to the source file on the host.
    remote_path : str
        Destination path on the device (e.g. ``/main.py``).
    chunk_size : int
        Raw bytes per base64 chunk (default 1024), before base64 encoding.
        Base64 adds ~33% overhead (1024 raw bytes → ~1366 over-the-wire
        bytes).  Each chunk generates a MicroPython script well under
        the device's heap/compile limit.

    Returns *True* on success.
    """
    try:
        with open(local_path, "rb") as fh:
            content = fh.read()
    except OSError as e:
        print(f"  ERROR: Cannot read {local_path}: {e}")
        return False

    file_size = len(content)
    remote_path = _prefix_path(remote_path)
    remote_path_esc = _sanitize_path_for_script(remote_path)

    # Step 0 — skip when the device already holds identical content.
    # MUST run before the remove/open steps below (both destroy the
    # existing device copy).
    if skip_if_unchanged:
        import hashlib

        local_sha = hashlib.sha256(content).hexdigest()
        if device_file_sha256(ser, remote_path) == local_sha:
            return "unchanged"

    # Step 1 — create parent directories.
    # Skipped when the parent IS the device flash root (DEVICE_PREFIX,
    # e.g. /flash) — that directory always exists, so the round trip is
    # pure overhead for top-level files like main.py / config.py.
    dirname = os.path.dirname(remote_path).replace("\\", "/")
    if dirname and dirname != "/" and dirname != DEVICE_PREFIX:
        safe_dirname = _sanitize_path_for_script(dirname)
        script = (
            b"import os as _os\n"
            b"_parts = '" + safe_dirname.encode("utf-8") + b"'.strip('/').split('/')\n"
            b"_path = ''\n"
            b"for _p in _parts:\n"
            b"    _path = _path + '/' + _p\n"
            b"    try:\n"
            b"        _os.mkdir(_path)\n"
            b"    except OSError as _e:\n"
            b"        if _e.args[0] != 17:  # EEXIST\n"
            b"            print('MKDIR_ERR:' + repr(_e))\n"
            b"            raise\n"
            b"print('DIR_OK')\n"
        )
        ok, _out = exec_raw(ser, script)
        if not ok:
            return False

    # Step 2 — remove existing file (ignore ENOENT)
    script = (
        b"import os as _os\n"
        b"try:\n"
        b"    _os.remove('" + remote_path_esc.encode("utf-8") + b"')\n"
        b"except OSError as _e:\n"
        b"    if _e.args[0] != 2:  # ENOENT\n"
        b"        print('REMOVE_ERR:' + repr(_e))\n"
        b"        raise\n"
        b"print('RM_OK')\n"
    )
    ok, _out = exec_raw(ser, script)
    if not ok:
        return False

    # Step 3 — open file, keep handle in global ``_lmao_f``
    script = b"_lmao_f = open('" + remote_path_esc.encode("utf-8") + b"', 'wb')\nprint('OPEN_OK')\n"
    ok, _out = exec_raw(ser, script)
    if not ok or "OPEN_OK" not in _out:
        return False

    # Step 4 — stream each chunk (one exec_raw per chunk), retrying a
    # failed chunk up to _STALL_LIMIT times before declaring the device
    # wedged.  A failed chunk is re-sent in full (the device only writes
    # when it acknowledges with CHUNK_OK, so retries cannot corrupt).
    offset = 0
    consecutive_failures = 0
    while offset < file_size:
        chunk = content[offset : offset + chunk_size]
        encoded = base64.b64encode(chunk).decode("ascii")
        chunk_script = (
            b"import ubinascii as _b64\n"
            b"_lmao_f.write(_b64.a2b_base64('" + encoded.encode("utf-8") + b"'))\n"
            b"print('CHUNK_OK')\n"
        )
        ok, _out = exec_raw(ser, chunk_script)
        if ok and "CHUNK_OK" in _out:
            consecutive_failures = 0
            offset += chunk_size
            continue

        consecutive_failures += 1
        if consecutive_failures >= _STALL_LIMIT:
            # Device appears wedged — close the dangling handle and bail out.
            try:
                ser.write(b"_lmao_f.close()\n")
                ser.write(b"\x04")
                time.sleep(0.3)
                ser.read(ser.in_waiting)
            except (serial.SerialException, OSError):
                pass
            raise DeviceStalledError(
                f"Device stopped responding at byte {offset} of {file_size} "
                f"while uploading {remote_path}. The USB-Serial-JTAG interface "
                "may be wedged — press the Cardputer's RESET button (or "
                "power-cycle it), then retry."
            )

    # Step 5 — close file and report success
    script = b"_lmao_f.close()\nprint('UPLOAD_OK')\n"
    ok, out = exec_raw(ser, script)
    if ok and "UPLOAD_OK" in out:
        return True

    if ok:
        print(f"  [raw output] {out[:300]}")
    return False


def _mip_install(ser, package):
    """Install a MicroPython package via mip on the connected device.

    Runs ``import mip; mip.install()`` in raw REPL and waits for completion.
    Skips silently if already installed or WiFi is unavailable (non-fatal).
    """
    try:
        script = (
            b"import mip\n"
            b"try:\n"
            b"  mip.install('" + package.encode("utf-8") + b"')\n"
            b"  print('MIP_OK')\n"
            b"except Exception as e:\n"
            b"  print('MIP_FAIL:', e)\n"
        )
        ok, out = exec_raw(ser, script, timeout=30)
        if ok and "MIP_OK" in out:
            print(f"  mip: {package} OK")
            return True
        if "already installed" in out.lower() or "exists" in out.lower():
            print(f"  mip: {package} already installed")
            return True
        print(f"  mip: {package} skipped (no network? {out[:100]})")
    except Exception as e:
        print(f"  mip: {package} skipped ({e})")
    return False


# ---- Main (entry-point for ``bazel run``) ----


DEVICE_PREFIX = "/flash"  # M5Stack firmware mounts flash at /flash


def _prefix_path(remote_path: str) -> str:
    """Prefix a remote path with the device's flash mount point."""
    remote_path = remote_path.replace("\\", "/")
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path
    # Strip leading slash and prefix with DEVICE_PREFIX
    while remote_path.startswith("/"):
        remote_path = remote_path[1:]
    return DEVICE_PREFIX + "/" + remote_path


def main():
    parser = argparse.ArgumentParser(
        description="Flash Cardputer with LMAO MicroPython client code",
    )
    parser.add_argument(
        "--port",
        "-p",
        default=None,
        help="Serial port (e.g. /dev/ttyACM0). Auto-detected when omitted.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify device type, do not upload files.",
    )
    parser.add_argument(
        "--client-root",
        default=None,
        help="Path to cardputer_client/ directory (auto-detected when omitted).",
    )
    args = parser.parse_args()

    # Locate the client source directory
    client_root = args.client_root or find_client_root()
    if not client_root:
        print("ERROR: Cannot find cardputer_client/ directory.")
        print("Are you running from the LMAO workspace root?")
        print("Specify with: --client-root /path/to/cardputer_client")
        sys.exit(1)

    # Auto-discover library files to upload (walks lib/ directory)
    lib_files = auto_discover_lib_files(client_root)

    # Verify all files exist before opening the serial port
    try:
        verify_files_exist(client_root, FILES_TO_UPLOAD + lib_files)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Find the Cardputer serial port
    port = find_cardputer_port(args.port)
    if not port:
        print("ERROR: Could not find Cardputer. Is it connected via USB?")
        print("Specify port manually with: --port /dev/ttyACM0")
        print("\nDetected serial ports:")
        for p in serial.tools.list_ports.comports():
            print(
                f"  {p.device} — {p.description}  (VID:0x{p.vid:04X})"
                if p.vid
                else f"  {p.device} — {p.description}"
            )
        sys.exit(1)

    print(f"Connecting to Cardputer on {port} …")

    try:
        ser = serial.Serial(port, 115200, timeout=1, write_timeout=10)
        # Give the device a moment after DTR/RTS toggle
        time.sleep(0.6)
    except serial.SerialException as e:
        print(f"ERROR: Cannot open {port}: {e}")
        sys.exit(1)

    try:
        # — Enter raw REPL —
        print("Entering MicroPython raw REPL …")
        if not enter_raw_repl(ser):
            print("ERROR: Could not enter raw REPL.")
            print("Is MicroPython firmware installed on the Cardputer?")
            print("The device might be in bootloader mode or running a different firmware.")
            sys.exit(1)

        # — Verify device —
        print("Verifying device …")
        ok, info = verify_device(ser)
        if ok:
            print(f"  {info}")
        else:
            print(f"WARNING: Device verification returned: {info}")
            print("Proceeding anyway — the port might still be correct.")

        if args.verify_only:
            print("Verification complete (--verify-only).")
            return

        # — Upload files —
        total_files = len(FILES_TO_UPLOAD) + len(lib_files)
        print(f"Uploading {total_files} file(s) …")
        _upload_files(ser, client_root, FILES_TO_UPLOAD)
        _upload_files(ser, client_root, lib_files)

        # — Install dependencies via mip —
        print("\nInstalling MicroPython dependencies …")
        _mip_install(ser, "lora-sx126x")
        _mip_install(ser, "lora-sync")
        _mip_install(ser, "contextlib")

        # — Soft reset —
        print("\nFlash complete. Soft-resetting Cardputer …")
        exit_raw_repl(ser)
        ser.write(b"\x04")  # Ctrl+D = soft reset in friendly REPL

        print("Done. The Cardputer will reboot and run the LMAO client automatically.")
        print(f"Uploaded {len(FILES_TO_UPLOAD)} client + {len(lib_files)} library file(s).")

    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)
    except serial.SerialException as e:
        print(f"\nERROR: Lost connection to Cardputer: {e}")
        print("Check the USB cable and try again.")
        sys.exit(1)
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"\nERROR: Unexpected error during flashing: {e}")
        sys.exit(1)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
