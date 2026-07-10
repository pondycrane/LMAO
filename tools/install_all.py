"""
Install LMAO client/services to all connected USB hardware.

Auto-detects connected devices (Cardputer, Heltec RNode) and installs the
appropriate software to each.  Runs detection and flashing in a single pass,
then prints a summary table of per-device results.

Usage (via Bazel):
    bazel run //tools:install_all
    bazel run //tools:install_all -- --cardputer-port /dev/ttyACM0
    bazel run //tools:install_all -- --rnode-port /dev/ttyUSB0
    bazel run //tools:install_all -- --skip-cardputer
    bazel run //tools:install_all -- --skip-rnode

Prerequisites:
    - Cardputer with MicroPython installed, connected via USB
    - Heltec ESP32 connected via USB (for RNode firmware)
    - ``rns`` Python package installed (provides ``rnodeconf``)
    - User has permissions on serial ports (dialout group)
"""

import argparse
import os
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Error: pyserial is required. Install with: pip install pyserial")
    sys.exit(1)

# Cardputer flashing helpers (from cardputer_client/flash.py via flash_lib).
from cardputer_client.flash import (
    FILES_TO_UPLOAD,
    auto_discover_lib_files,
    enter_raw_repl,
    exit_raw_repl,
    find_cardputer_port,
    find_client_root,
    upload_file,
    verify_device,
    verify_files_exist,
)

# RNode firmware helpers (from tests/e2e/e2e_helpers.py via e2e_helpers_lib).
from tests.e2e.e2e_helpers import (
    check_rnode_firmware,
    find_rnode_port,
    flash_rnode_firmware,
)

# Server-service install helpers (from tools/install_services.py).
from tools.install_services import install_pi_server, install_k8s_services

# ---- Result tracking ----


class DeviceResult:
    """Captures the outcome of a device flash/install operation."""

    def __init__(self, name: str):
        self.name = name
        self.status = "SKIP"  # SKIP | OK | FAIL
        self.detail = ""

    def ok(self, detail: str = "") -> None:
        self.status = "OK"
        self.detail = detail

    def fail(self, detail: str = "") -> None:
        self.status = "FAIL"
        self.detail = detail

    def skip(self, reason: str = "") -> None:
        self.status = "SKIP"
        self.detail = reason


# ---- Cardputer operations ----


def _flash_cardputer_client(port: str, client_root: str, result: DeviceResult) -> None:
    """Flash the LMAO MicroPython client to a Cardputer on *port*.

    Opens the serial connection, enters raw REPL, verifies the device,
    uploads all client and library files, then exits raw REPL.

    On failure the *result* is updated with ``FAIL`` and a diagnostic
    message; the serial port is always closed in the ``finally`` block.
    """
    try:
        print(f"\n--- Cardputer: opening {port} ---")
        ser = serial.Serial(port, 115200, timeout=1)
        time.sleep(0.6)
    except serial.SerialException as exc:
        result.fail(f"Cannot open serial port {port}: {exc}")
        print(f"  FAIL: {exc}")
        return

    try:
        # Enter MicroPython raw REPL.
        print("  Entering MicroPython raw REPL ...")
        if not enter_raw_repl(ser):
            result.fail("Could not enter raw REPL (is MicroPython installed?)")
            print("  FAIL: Could not enter raw REPL")
            return

        # Verify the device is an ESP32.
        print("  Verifying device ...")
        ok, info = verify_device(ser)
        if ok:
            print(f"    {info}")
        else:
            print(f"    WARNING: {info}")

        # Discover library files.
        lib_files = auto_discover_lib_files(client_root)
        all_files = list(FILES_TO_UPLOAD) + lib_files

        # Verify source files exist on host.
        try:
            verify_files_exist(client_root, all_files)
        except FileNotFoundError as exc:
            result.fail(f"Missing source file: {exc}")
            print(f"  FAIL: {exc}")
            return

        # Upload each file.
        total = len(all_files)
        print(f"  Uploading {total} file(s) ...")
        failed = 0
        for rel in all_files:
            local_path = os.path.join(client_root, rel)
            size = os.path.getsize(local_path)
            print(f"    {rel:35s} … ", end="", flush=True)
            if upload_file(ser, local_path, rel):
                print(f"OK  ({size} B)")
            else:
                print("FAILED")
                failed += 1

        if failed > 0:
            result.fail(f"{failed} of {total} file(s) failed to upload")
            print(f"  FAIL: {failed}/{total} files failed")
            return

        # Soft reset.
        print("  Soft-resetting Cardputer ...")
        exit_raw_repl(ser)
        ser.write(b"\x04")  # Ctrl+D = soft reset

        result.ok(f"Flashed {total} file(s) to Cardputer")
        print(f"  OK: {total} file(s) uploaded")

    except KeyboardInterrupt:
        result.fail("Aborted by user")
        print("\n  Interrupted by user — Cardputer flash cancelled.")
        return
    except serial.SerialException as exc:
        result.fail(f"Serial error: {exc}")
        print(f"  FAIL: {exc}")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error: {exc}")
        print(f"  FAIL: {exc}")
    finally:
        ser.close()


# ---- RNode operations ----


def _install_rnode_firmware(port: str, result: DeviceResult) -> None:
    """Check and (if needed) flash RNode firmware onto a Heltec at *port*.

    Delegates to ``e2e_helpers.check_rnode_firmware`` and
    ``e2e_helpers.flash_rnode_firmware`` which wrap ``rnodeconf``.
    """
    print(f"\n--- RNode: checking firmware on {port} ---")

    try:
        # Step 1 — check if RNode firmware is already present.
        is_rnode = check_rnode_firmware(port)
        if is_rnode:
            result.ok(f"RNode firmware already installed on {port}")
            print("  OK: RNode firmware already detected")
            return

        # Step 2 — not an RNode; trigger autoinstall.
        print("  RNode firmware not detected. Starting autoinstall ...")
        success, message = flash_rnode_firmware(port)
        if success:
            result.ok(f"RNode firmware flashed: {message}")
            print(f"  OK: {message}")
        else:
            result.fail(message)
            print(f"  FAIL: {message}")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during RNode flashing: {exc}")
        print(f"  FAIL: {exc}")


# ---- Summary output ----


def _print_summary(results: list[DeviceResult]) -> None:
    """Print a per-device success/failure summary table and exit.

    Exits with code 0 when all devices succeeded (or were skipped).
    Exits with code 1 when any device failed.
    """
    print("\n" + "=" * 60)
    print("  INSTALL SUMMARY")
    print("=" * 60)

    label_width = max(len(r.name) for r in results) if results else 0
    label_width = max(label_width, 10)

    any_fail = False
    for r in results:
        tag = f"[{r.status}]"
        line = f"  {tag:6s}  {r.name:<{label_width}s}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)
        if r.status == "FAIL":
            any_fail = True

    print("=" * 60)

    if any_fail:
        print("  One or more devices FAILED. See above for details.")
        sys.exit(1)
    else:
        print("  All detected devices processed successfully.")
        sys.exit(0)


# ---- Argument parsing ----


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Install LMAO client/services to all connected USB hardware",
    )
    parser.add_argument(
        "--cardputer-port",
        default=None,
        help="Serial port for Cardputer (e.g. /dev/ttyACM0). Auto-detected when omitted.",
    )
    parser.add_argument(
        "--rnode-port",
        default=None,
        help="Serial port for RNode/Heltec (e.g. /dev/ttyUSB0). Auto-detected when omitted.",
    )
    parser.add_argument(
        "--skip-cardputer",
        action="store_true",
        help="Skip Cardputer detection and flashing entirely.",
    )
    parser.add_argument(
        "--skip-rnode",
        action="store_true",
        help="Skip RNode detection and flashing entirely.",
    )
    parser.add_argument(
        "--client-root",
        default=None,
        help="Path to cardputer_client/ directory (auto-detected when omitted).",
    )
    parser.add_argument(
        "--include-services",
        action="store_true",
        help="Also install Pi server (Docker) and apply K8s manifests.",
    )
    parser.add_argument(
        "--skip-server",
        action="store_true",
        help="Skip Pi server Docker build/run (only meaningful with --include-services).",
    )
    parser.add_argument(
        "--skip-k8s",
        action="store_true",
        help="Skip Kubernetes manifest apply (only meaningful with --include-services).",
    )
    return parser.parse_args(argv)


# ---- Main entry-point ----


def main(argv: list[str] | None = None) -> None:
    """Run the install-all pipeline: detect hardware, flash each device, print summary."""
    args = _parse_args(argv)

    results: list[DeviceResult] = []

    # ── Cardputer ──
    cp_result = DeviceResult("Cardputer")
    results.append(cp_result)

    if args.skip_cardputer:
        cp_result.skip("--skip-cardputer")
        print("Cardputer: SKIP (--skip-cardputer)")
    else:
        client_root = args.client_root or find_client_root()
        if not client_root:
            cp_result.fail(
                "Cannot locate cardputer_client/ directory. Specify with --client-root."
            )
            print("Cardputer: FAIL — cannot locate cardputer_client/ directory")
        else:
            port = find_cardputer_port(args.cardputer_port)
            if not port:
                cp_result.skip("No Cardputer detected on USB")
                print("Cardputer: SKIP — not detected on USB")
            else:
                _flash_cardputer_client(port, client_root, cp_result)

    # ── RNode ──
    rn_result = DeviceResult("RNode (Heltec)")
    results.append(rn_result)

    if args.skip_rnode:
        rn_result.skip("--skip-rnode")
        print("RNode: SKIP (--skip-rnode)")
    else:
        if args.rnode_port:
            port = args.rnode_port
        else:
            port = find_rnode_port()
        if not port:
            rn_result.skip("No RNode/Heltec detected on USB")
            print("RNode: SKIP — not detected on USB")
        else:
            _install_rnode_firmware(port, rn_result)

    # ── Services (Pi server + K8s) ──
    pi_result = DeviceResult("Pi Server")
    results.append(pi_result)
    k8s_result = DeviceResult("K8s Services")
    results.append(k8s_result)

    if args.include_services:
        if args.skip_server:
            pi_result.skip("--skip-server")
        else:
            install_pi_server(pi_result)

        if args.skip_k8s:
            k8s_result.skip("--skip-k8s")
        else:
            install_k8s_services(k8s_result)
    else:
        pi_result.skip("--include-services not set")
        k8s_result.skip("--include-services not set")

    # ── Summary ──
    _print_summary(results)


if __name__ == "__main__":
    main()
