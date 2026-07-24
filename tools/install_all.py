"""
Install LMAO client/services to all connected USB hardware.

Auto-detects connected devices (Cardputer, Heltec RNode) and installs the
appropriate software to each.  Runs detection and flashing in a single pass,
then prints a summary table of per-device results.

When --include-services is set, also builds the Pi server Docker image and
applies Kubernetes manifests to the cluster.

When --setup-registry is set, starts the local Docker registry and pushes
all LMAO images to it.

Usage (via Bazel):
    bazel run //tools:install_all
    bazel run //tools:install_all -- --cardputer-port /dev/ttyACM0
    bazel run //tools:install_all -- --rnode-port /dev/ttyUSB0
    bazel run //tools:install_all -- --skip-cardputer
    bazel run //tools:install_all -- --skip-rnode
    bazel run //tools:install_all -- --setup-registry
    bazel run //tools:install_all -- --include-services
    bazel run //tools:install_all -- --include-services --skip-k8s

Prerequisites:
    - Cardputer with MicroPython installed, connected via USB
    - Heltec ESP32 connected via USB (for RNode firmware)
    - ``esptool`` Python package installed (for ESP32 flashing)
    - ``pyserial`` Python package installed
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
    DeviceStalledError,
    _mip_install,
    auto_discover_lib_files,
    disarm_watchdog,
    enter_raw_repl,
    exit_raw_repl,
    find_cardputer_port,
    find_client_root,
    recover_wedged_device,
    upload_file,
    verify_device,
    verify_files_exist,
)

# RNode firmware helpers (manual flash only — see rnode_firmware/README.md).
# The Heltec RNode must be flashed manually via the web tool.
# (No import needed — probe is done via the RNode serial protocol directly.)

# Server-service install helpers (from tools/install_services.py).
from tools.install_services import (
    _docker_psql,
    detect_serial_devices,
    install_iot_ingest_consumer,
    install_k8s_services,
    install_pi_server,
    run_pi_server,
    setup_registry,
    stop_pi_server_container,
)

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
        ser = serial.Serial(port, 115200, timeout=1, write_timeout=10)
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

        # Extend any client-armed hardware watchdog so it cannot reset
        # the device mid-install (the LMAO client arms one at boot).
        disarm_watchdog(ser)

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

        # Upload each file (skipping files already identical on the device).
        # On a wedged device (raw REPL OK but writes stall — issue #74),
        # attempt one automatic recovery before failing the install.
        total = len(all_files)
        print(f"  Uploading {total} file(s) ...")
        failed = 0
        recovery_attempted = False
        for rel in all_files:
            local_path = os.path.join(client_root, rel)
            size = os.path.getsize(local_path)
            print(f"    {rel:35s} … ", end="", flush=True)
            try:
                res = upload_file(ser, local_path, rel, skip_if_unchanged=True)
            except DeviceStalledError as exc:
                if recovery_attempted:
                    print("FAILED")
                    result.fail(str(exc))
                    print(f"  FAIL: {exc}")
                    return
                recovery_attempted = True
                new_ser = recover_wedged_device(ser, port)
                if new_ser is None:
                    print("FAILED")
                    result.fail(str(exc))
                    print(f"  FAIL: {exc}")
                    print("  Automatic recovery failed — press the Cardputer's "
                          "RESET button (or power-cycle it), then retry.")
                    return
                ser = new_ser
                disarm_watchdog(ser)
                print("RECOVERED — retrying ", end="", flush=True)
                try:
                    res = upload_file(ser, local_path, rel, skip_if_unchanged=True)
                except DeviceStalledError as exc2:
                    print("FAILED")
                    result.fail(str(exc2))
                    print(f"  FAIL: {exc2}")
                    return
            if res == "unchanged":
                print("OK  (unchanged)")
            elif res:
                print(f"OK  ({size} B)")
            else:
                print("FAILED")
                failed += 1

        if failed > 0:
            result.fail(f"{failed} of {total} file(s) failed to upload")
            print(f"  FAIL: {failed}/{total} files failed")
            return

        # Install MicroPython dependencies (lora driver, contextlib).
        print("  Installing MicroPython dependencies ...")
        _mip_install(ser, "lora-sx126x")
        _mip_install(ser, "lora-sync")
        _mip_install(ser, "contextlib")

        # Soft reset.
        print("  Soft-resetting Cardputer ...")
        exit_raw_repl(ser)
        ser.write(b"\x04")  # Ctrl+D = soft reset

        result.ok(f"Flashed {total} file(s) + dependencies to Cardputer")
        print(f"  OK: {total} file(s) uploaded, dependencies installed")

    except KeyboardInterrupt:
        result.fail("Aborted by user")
        print("\n  Interrupted by user — Cardputer flash cancelled.")
        return
    except DeviceStalledError as exc:
        result.fail(str(exc))
        print(f"  FAIL: {exc}")
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


def _rnode_probe_hint() -> str:
    """Return a diagnostic hint when the lmao-server container is running.

    A running server holds the RNode serial port and races the DETECT
    probe (async LoRa KISS frames interleave with the probe response).
    """
    try:
        import shutil as _shutil

        if _shutil.which("docker") and _docker_psql("name=lmao-server"):
            return (
                " HINT: the lmao-server container is running and holds the RNode "
                "port, which races this probe. Stop it first "
                "(docker stop lmao-server) or re-run with --include-services "
                "(stops and redeploys it)."
            )
    except Exception:
        pass
    return ""


def _install_rnode_firmware(port: str, result: DeviceResult) -> None:
    """Check if a Heltec at *port* is running RNode firmware.

    Uses :func:`lma_core.device_detect.probe_rnode` (the RNode DETECT
    protocol 0x08 + 0x73 signature). Does NOT flash programmatically —
    RNode firmware must be installed via the web flasher tool
    (see rnode_firmware/README.md).
    """
    print(f"\n--- RNode: checking firmware on {port} ---")

    try:
        from lma_core.device_detect import probe_rnode

        if probe_rnode(port):
            result.ok(f"RNode firmware detected on {port}")
            print("  OK: RNode firmware detected (DETECT signature confirmed)")
            return

    except ImportError:
        pass

    # Fallback: inline probe
    try:
        import serial as _serial

        ser = _serial.Serial(port, 115200, timeout=2)
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.write(bytes([0xC0, 0x08, 0x73, 0xC0]))
        time.sleep(0.5)
        data = ser.read(100)
        ser.close()

        if len(data) >= 4 and data[0:1] == b"\xC0" and data[1] == 0x08 and data[2] == 0x46:
            result.ok(f"RNode firmware detected on {port}")
            print("  OK: RNode firmware detected (DETECT signature confirmed)")
        elif len(data) > 0:
            hint = _rnode_probe_hint()
            result.fail(
                f"Device on {port} responded but not as RNode. "
                f"Response: {data.hex()}{hint}"
            )
            print(f"  FAIL: Unexpected response \u2014 {data.hex()}{hint}")
        else:
            hint = _rnode_probe_hint()
            result.fail(
                f"Device on {port} is not responding as RNode. "
                f"Use the web flasher tool: https://flasher.rnode.ams1.meshkube.com/"
                f"\n    See rnode_firmware/README.md for instructions.{hint}"
            )
            print("  FAIL: Not an RNode \u2014 manual flash required")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"RNode probe failed: {exc}")
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
    """Parse command-line arguments.

    Returns:
        argparse.Namespace with all parsed flags.
    """
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
        "--setup-registry",
        action="store_true",
        help="Start local Docker registry and push LMAO images.",
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
    parser.add_argument(
        "--skip-iot-ingest",
        action="store_true",
        help="Skip IoT Ingest Consumer deploy (only meaningful with --include-services).",
    )
    return parser.parse_args(argv)


# ---- Main entry-point ----


def main(argv: list[str] | None = None) -> None:
    """Run the install-all pipeline.

    Detects hardware, flashes each device, optionally deploys services,
    and prints a per-device summary table.
    """
    args = _parse_args(argv)

    # When services will be (re)deployed, stop the running lmao-server
    # container first so it does not hold the RNode serial port during
    # hardware probing (a running server races the RNode DETECT probe).
    if args.include_services and not args.skip_server:
        stop_pi_server_container()

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
            cp_result.fail("Cannot locate cardputer_client/ directory. Specify with --client-root.")
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
        port = None
        if args.rnode_port:
            port = args.rnode_port
        else:
            # Use shared device detection library
            try:
                from lma_core.device_detect import find_rnode_port

                port = find_rnode_port()
            except ImportError:
                # Fallback to detect_serial_devices
                rnode_auto, _ = detect_serial_devices()
                port = rnode_auto

        if not port:
            rn_result.skip("No RNode/Heltec detected on USB")
            print("RNode: SKIP — not detected on USB")
        else:
            _install_rnode_firmware(port, rn_result)

    # ── Local Registry ──
    registry_result = DeviceResult("Local Registry")
    results.append(registry_result)
    registry_ready = False

    if args.setup_registry:
        try:
            setup_registry(registry_result)
            if registry_result.status == "OK":
                registry_ready = True
        except Exception as exc:
            import traceback

            traceback.print_exc()
            registry_result.fail(f"Registry setup error: {exc}")
    else:
        registry_result.skip("--setup-registry not set")

    # ── Services (Pi server + K8s) ──
    pi_result = DeviceResult("Pi Server")
    results.append(pi_result)
    k8s_result = DeviceResult("K8s Services")
    results.append(k8s_result)
    iot_result = DeviceResult("IoT Ingest Consumer")
    results.append(iot_result)

    if args.include_services:
        try:
            if args.skip_server:
                pi_result.skip("--skip-server")
            else:
                install_pi_server(pi_result)
                if pi_result.status == "OK":
                    run_pi_server(pi_result)
                else:
                    print("  Skipping container deploy — image build/release did not succeed")

            if args.skip_k8s:
                k8s_result.skip("--skip-k8s")
            else:
                install_k8s_services(k8s_result)

            if args.skip_iot_ingest:
                iot_result.skip("--skip-iot-ingest")
            elif args.skip_k8s:
                iot_result.skip("--skip-k8s (K8s services skipped)")
            else:
                if args.setup_registry and not registry_ready:
                    print("  WARNING: registry setup did not succeed — iot-ingest push may fail")
                install_iot_ingest_consumer(iot_result)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            pi_result.fail(f"Pi Server install error: {exc}")
            k8s_result.fail(f"K8s Services install error: {exc}")
            iot_result.fail(f"IoT Ingest Consumer install error: {exc}")
    else:
        pi_result.skip("--include-services not set")
        k8s_result.skip("--include-services not set")
        iot_result.skip("--include-services not set")

    # ── Summary ──
    _print_summary(results)


if __name__ == "__main__":
    main()
