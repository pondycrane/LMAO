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
    "config.py",
    "main.py",
    "lora_boards.py",
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
    """Upload a list of files to the device, exiting on failure."""
    for rel in file_list:
        local_path = os.path.join(client_root, rel)
        print(f"  {rel:30s} … ", end="", flush=True)
        if upload_file(ser, local_path, rel):
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
        raise ValueError(
            f"Path contains backslash, cannot safely embed in script: {path!r}"
        )
    # Escape single quotes for the MicroPython string literal
    path = path.replace("'", "\\'")
    # Basic sanity: reject non-printable characters
    if not all(32 <= ord(c) < 127 for c in path):
        raise ValueError(
            f"Path contains non-printable characters: {path!r}"
        )
    return path


def _verify_files_exist(client_root, file_list):
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
    Otherwise all available serial ports are scanned for known Espressif USB
    VID (0x303A) or descriptive strings containing 'esp32', 'jtag', etc.
    """
    if preferred:
        return preferred

    try:
        ports = serial.tools.list_ports.comports()
    except Exception as e:
        print(f"WARNING: Could not enumerate serial ports: {e}")
        print("Specify the port manually with --port /dev/ttyACM0")
        return None

    for p in ports:
        try:
            if p.vid == 0x303A:  # Espressif
                return p.device
        except (TypeError, AttributeError):
            pass  # port object may not have a vid
        try:
            desc = (p.description or "").lower()
        except (TypeError, AttributeError):
            desc = ""
        if any(kw in desc for kw in ("esp32", "cp210x", "ch340", "jtag", "usb serial")):
            return p.device

    return None


def enter_raw_repl(ser):
    """Sends Ctrl+C (twice) + Ctrl+A to enter MicroPython raw REPL mode.

    Blocks until the ``raw REPL; CTRL-B to exit`` banner is received
    (or a 3-second timeout elapses).
    """
    try:
        # Interrupt anything that may be running
        ser.write(b"\r\x03\x03")
        time.sleep(0.3)
        ser.read(ser.in_waiting)  # drain any residual output

        # Request raw REPL
        ser.write(b"\r\x01")
        time.sleep(0.3)

        data = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            if ser.in_waiting:
                data += ser.read(ser.in_waiting)
            if b"raw REPL" in data:
                # Give device a moment to print the '>' prompt, then drain
                time.sleep(0.15)
                ser.read(ser.in_waiting)
                return True
            time.sleep(0.05)

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
    ok, out = exec_raw(ser, """
import sys as _sys
import os as _os
print(_sys.platform)
print(_os.uname().machine)
""")
    if not ok:
        return False, f"exec_raw failed: {out[:200]}"

    lines = [l for l in out.split("\n") if l and l != "OK"]
    platform = lines[0] if lines else ""
    machine = lines[1] if len(lines) > 1 else ""

    if "esp32" in platform.lower() or "esp32" in machine.lower():
        return True, f"ESP32 / {platform} / {machine}"
    return False, f"Not an ESP32 device — platform={platform!r}, machine={machine!r}"


def upload_file(ser, local_path, remote_path, chunk_size=2048):
    """Upload a single file to the MicroPython device using raw REPL.

    The file content is base64-encoded and sent in *chunk_size* byte pieces
    (measured as raw bytes of the source file, not base64-encoded size)
    to avoid hitting MicroPython parser limits.  The remote file is always
    written to ``/`` (device flash root), with any missing sub-directories
    created automatically.

    Returns *True* on success.
    """
    try:
        with open(local_path, "rb") as fh:
            content = fh.read()
    except OSError as e:
        print(f"  ERROR: Cannot read {local_path}: {e}")
        return False

    file_size = len(content)
    remote_path = remote_path.replace("\\", "/")
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path
    # Sanitize path for safe embedding in MicroPython string literals
    remote_path = _sanitize_path_for_script(remote_path)

    # Ensure the target directory exists (create all parent directories)
    dirname = os.path.dirname(remote_path).replace("\\", "/")
    mkdir_block = ""
    if dirname and dirname != "/":
        # Sanitize dirname before embedding
        safe_dirname = _sanitize_path_for_script(dirname)
        mkdir_block = f"""
_parts = '{safe_dirname}'.strip('/').split('/')
_path = ''
for _p in _parts:
    _path = _path + '/' + _p
    try:
        import os as _os
        _os.mkdir(_path)
    except OSError as _e:
        if _e.args[0] != 17:  # EEXIST is benign
            print('MKDIR_ERR:' + repr(_e))
            raise
"""

    # Build script: remove old file, open new, write in chunks, close
    script = f"""import ubinascii as _b64
{mkdir_block}
try:
    import os as _os
    _os.remove('{remote_path}')
except OSError as _e:
    if _e.args[0] != 2:  # ENOENT — file not found, that's OK
        print('REMOVE_ERR:' + repr(_e))
        raise
_f = open('{remote_path}', 'wb')
"""

    for offset in range(0, file_size, chunk_size):
        chunk = content[offset:offset + chunk_size]
        encoded = base64.b64encode(chunk).decode("ascii")
        script += f"_f.write(_b64.a2b_base64('{encoded}'))\n"

    script += "_f.close()\nprint('UPLOAD_OK')\n"

    ok, out = exec_raw(ser, script)
    if ok and "UPLOAD_OK" in out:
        return True

    # If we got OK but not UPLOAD_OK, the script may have errored
    if ok:
        print(f"  [raw output] {out[:300]}")
    return False


# ---- Main (entry-point for ``bazel run``) ----

def main():
    parser = argparse.ArgumentParser(
        description="Flash Cardputer with LMAO MicroPython client code",
    )
    parser.add_argument(
        "--port", "-p", default=None,
        help="Serial port (e.g. /dev/ttyACM0). Auto-detected when omitted.",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Only verify device type, do not upload files.",
    )
    parser.add_argument(
        "--client-root", default=None,
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
        _verify_files_exist(client_root, FILES_TO_UPLOAD + lib_files)
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
            print(f"  {p.device} — {p.description}  (VID:0x{p.vid:04X})" if p.vid else f"  {p.device} — {p.description}")
        sys.exit(1)

    print(f"Connecting to Cardputer on {port} …")

    try:
        ser = serial.Serial(port, 115200, timeout=1)
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

        # — Soft reset —
        print("\nFlash complete. Soft-resetting Cardputer …")
        exit_raw_repl(ser)
        ser.write(b"\x04")  # Ctrl+D = soft reset in friendly REPL

        print(f"Done. The Cardputer will reboot and run the LMAO client automatically.")
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
