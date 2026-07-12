"""Shared helpers for E2E tests (Cardputer flash, LoRa communication).

These helpers are imported by individual E2E test files to avoid code
duplication.  They require pyserial and physical hardware to be connected.
"""

import os
import subprocess
import sys
import traceback

import serial
import serial.tools.list_ports

# Known USB VID values for RNode-compatible devices.
# Consolidates VID checking into a single set to avoid verbose
# try/except-per-VID anti-pattern.
RNODE_VIDS = {0x303A, 0x10C4, 0x1A86}


def _find_system_python() -> str:
    """Return the path of a system Python that has ``rns`` installed.

    Bazel's sandbox Python (``sys.executable`` inside a test) does *not*
    have ``rns`` available, so we resolve a system Python via
    ``shutil.which()`` with a preference for the user-local install.

    Returns:
        Absolute path to a Python interpreter that can import ``RNS``,
        or ``sys.executable`` as a fallback (which will fail at runtime
        but preserves the existing error path for non-Bazel invocations).
    """
    import shutil

    # Preference order: user-local pip, then system python3, then python.
    candidates = [
        os.path.expanduser("~/.local/bin/python3"),
        shutil.which("python3"),
        shutil.which("python"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "import RNS; print(RNS.__version__)"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            continue

    # Fallback — will likely produce the same ModuleNotFoundError as before.
    return sys.executable


def case_insensitive_contains(haystack: bytes, needle: str) -> bool:
    """Check if *needle* appears in *haystack*, case-insensitively.

    Both arguments are lowercased before comparison so that
    e.g. ``case_insensitive_contains(b"ACK received", "ack")`` returns ``True``.

    Args:
        haystack: Byte string to search within.
        needle: Plain-text substring to search for (will be encoded as ASCII).

    Returns:
        ``True`` if *needle* (lowercased) appears in *haystack* (lowercased).
    """
    return needle.encode().lower() in haystack.lower()


def find_rnode_port():
    """Return the device path of a connected Heltec/ESP32 RNode, or *None*.

    RNode devices appear as USB serial (CP210x, CH340, or Espressif USB).
    Also checks for "rnode" in the description string.
    """
    try:
        ports = serial.tools.list_ports.comports()
    except Exception as exc:
        print(f"WARNING: Could not enumerate serial ports: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None

    for p in ports:
        try:
            if p.vid in RNODE_VIDS:
                return p.device
        except (TypeError, AttributeError) as exc:
            print(f"DEBUG: skipping port {getattr(p, 'device', '<unknown>')}: {exc}")
        try:
            desc = (p.description or "").lower()
        except (TypeError, AttributeError) as exc:
            print(
                f"DEBUG: could not read description for {getattr(p, 'device', '<unknown>')}: {exc}"
            )
            desc = ""
        if "rnode" in desc:
            return p.device

    return None


def check_rnode_firmware(port: str, timeout: int = 15) -> bool:
    """Check whether a Heltec on *port* is running RNode firmware.

    Calls ``python -m RNS.Utilities.rnodeconf PORT --info`` (port is a
    positional argument) and inspects the output for tell-tale signs of
    RNode firmware.

    Args:
        port: Device path (e.g. ``/dev/ttyUSB0``).
        timeout: Seconds to wait for the subprocess to complete.

    Returns:
        ``True`` if the port responds as an RNode, ``False`` otherwise
        (including when ``rnodeconf`` is not importable or the subprocess
        times out).
    """
    try:
        system_python = _find_system_python()
        result = subprocess.run(
            [
                system_python,
                "-m",
                "RNS.Utilities.rnodeconf",
                port,
                "--info",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        print(
            "WARNING: Cannot run rnodeconf -- python interpreter not found.",
            file=sys.stderr,
        )
        return False
    except subprocess.TimeoutExpired as exc:
        print(
            f"WARNING: rnodeconf --info timed out after {timeout}s: {exc}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"WARNING: rnodeconf --info failed for {port}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return False

    if result.returncode != 0:
        return False

    # A successful rnodeconf --info prints device details; look for
    # expected firmware signature strings.
    combined = (result.stdout + result.stderr).lower()
    return "rnode firmware" in combined or "lora:" in combined


def flash_rnode_firmware(port: str, timeout: int = 120) -> tuple[bool, str]:
    """Flash RNode firmware onto the Heltec at *port* via ``rnodeconf --autoinstall``.

    Performs an automated firmware install that erases and re-flashes the
    ESP32.  This can take 60-90 seconds; the default timeout is 120 s.

    Args:
        port: Device path (e.g. ``/dev/ttyUSB0``).
        timeout: Seconds to wait for flashing to complete.

    Returns:
        A ``(success, message)`` tuple.  *success* is ``True`` when the
        subprocess exits 0; *message* contains the captured output or the
        error summary.
    """
    print(f"\nAuto-flashing RNode firmware on {port} ...", flush=True)
    try:
        system_python = _find_system_python()
        result = subprocess.run(
            [
                system_python,
                "-m",
                "RNS.Utilities.rnodeconf",
                port,
                "--autoinstall",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        msg = "Cannot run rnodeconf -- python interpreter not found. Is rns installed?"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except subprocess.TimeoutExpired as exc:
        msg = (
            f"rnodeconf --autoinstall timed out after {timeout}s. "
            f"The Heltec may be stuck in bootloader mode: {exc}"
        )
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)
    except Exception as exc:
        msg = f"rnodeconf --autoinstall on {port} failed: {exc}"
        print(f"WARNING: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return (False, msg)

    if result.returncode != 0:
        stderr_tail = [line for line in result.stderr.strip().split("\n") if line][-5:]
        if stderr_tail:
            msg = "Flash failed: " + "\n".join(stderr_tail)
        else:
            msg = f"rnodeconf exited {result.returncode}"
        print(f"WARNING: {msg}", file=sys.stderr)
        return (False, msg)

    print("Flash successful.", flush=True)
    return (True, "Flash successful")
