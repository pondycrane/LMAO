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
import re
import sys
import tempfile
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
    deploy_mpy_bytecode,
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

# Server identity helpers — load-or-create the persisted server identity
# and derive the lxmf.delivery destination hash clients use as DEST_HASH.
from lma_core.server_identity import ensure_delivery_destination_hash

# Server-service install helpers (from tools/install_services.py).
from tools.install_services import (
    _docker_psql,
    deploy_lmao_server,
    detect_serial_devices,
    install_iot_ingest_consumer,
    install_k8s_services,
    install_pi_server,
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


def _flash_cardputer_native(port: str, result: DeviceResult,
                            inject_dest_hash: bool = True) -> None:
    """Build + flash the native C Cardputer firmware (PR1 sensor node).

    The firmware lives in cardputer_client/firmware/.  build.sh stages the app
    into the shared RTReticulum checkout and runs the ESP-IDF Docker build;
    flash.sh runs ``idf.py flash`` on *port*.  DEST_HASH is baked in at build
    time via LMAO_DEST_HASH_HEX (the source tree is never modified), mirroring
    how the MicroPython client had config.py patched at flash time.

    Requires: Docker (espressif/idf:v5.3.1) on the host.  The Cardputer is
    flashed in download mode via idf.py — superseding the raw-REPL-only rule
    of the MicroPython era (the device no longer runs MicroPython).
    """
    import subprocess

    # Prefer the real workspace: under `bazel run` this file lives in the
    # runfiles tree, where build.sh's relative staging of
    # firmware_common/ + the shared .rtreticulum checkout would not resolve
    # (it would clone RTReticulum from the network at an unpinned revision).
    # Same resolution the E2E tests use.
    firmware_dir = None
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        cand = os.path.join(workspace, "cardputer_client", "firmware")
        if os.path.isfile(os.path.join(cand, "build.sh")):
            firmware_dir = cand
    if firmware_dir is None:
        firmware_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cardputer_client",
            "firmware",
        )
    build_sh = os.path.join(firmware_dir, "build.sh")
    flash_sh = os.path.join(firmware_dir, "flash.sh")
    for path, what in ((build_sh, "build script"), (flash_sh, "flash script")):
        if not os.path.isfile(path):
            result.fail(f"Missing {what}: {path}")
            print(f"  FAIL: missing {what} {path}")
            return

    env = dict(os.environ)
    env["PORT"] = port  # flash.sh maps --port to FLASH_PORT semantics below
    if inject_dest_hash:
        print("  Computing server DEST_HASH ...")
        try:
            dest_hash = ensure_delivery_destination_hash()
            env["LMAO_DEST_HASH_HEX"] = dest_hash
            print(f"    DEST_HASH = {dest_hash}")
        except Exception as exc:
            result.fail(f"DEST_HASH resolution failed: {exc}")
            print(f"  FAIL: DEST_HASH resolution failed — {exc}")
            return

    print(f"\n--- Cardputer (native): building firmware on {port} ---")
    try:
        # Build with the baked-in DEST_HASH (build.sh stages + Docker idf.py).
        proc = subprocess.run(
            ["bash", build_sh],
            env=env,
            cwd=firmware_dir,
            text=True,
            stdout=subprocess.DEVNULL if not os.environ.get("LMAO_VERBOSE") else None,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            result.fail(f"Native firmware build failed (exit {proc.returncode})")
            print("  FAIL: native firmware build failed — run "
                  "bazel run //cardputer_client:build_firmware for the full log")
            return
    except FileNotFoundError as exc:
        result.fail(f"build.sh failed to run (bash? subprocess): {exc}")
        print(f"  FAIL: {exc}")
        return
    except Exception as exc:
        result.fail(f"Native firmware build error: {exc}")
        print(f"  FAIL: {exc}")
        return

    # The ESP-IDF build takes minutes (Docker + full reconfigure).  A
    # USB-Serial-JTAG console does not reliably survive that window on this
    # board: it can drop off the bus, or reappear on a different node.  The
    # port was resolved before the build, so re-resolve it now — otherwise the
    # flash dies with Docker's cryptic "error gathering device information ...
    # no such file or directory" for a device that a replug would restore.
    if not os.path.exists(port):
        refreshed = find_cardputer_port()
        if refreshed and refreshed != port:
            print(f"  NOTE: Cardputer is now on {refreshed} (was {port})")
            port = refreshed
            env["PORT"] = port
    if not os.path.exists(port):
        result.fail(f"Cardputer not on USB after the build ({port})")
        print(f"  FAIL: {port} disappeared during the build — replug the "
              "Cardputer and re-run; the image is already built, so\n"
              "        bazel run //cardputer_client:flash_firmware -- "
              f"--port {port}\n"
              "        flashes it without another build.")
        return

    print(f"--- Cardputer (native): flashing to {port} ---")
    try:
        # flash.sh: exec docker ... idf.py -p /dev/ttyACM0 -b BAUD flash
        # FLASH_PORT selects the device so the script maps /dev/ttyACM0.
        env["FLASH_PORT"] = port
        env["FLASH_BAUD"] = env.get("FLASH_BAUD", "921600")
        proc = subprocess.run(
            ["bash", flash_sh, "--port", port],
            env=env,
            cwd=firmware_dir,
            text=True,
            stdout=subprocess.DEVNULL if not os.environ.get("LMAO_VERBOSE") else None,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            result.fail(f"Native firmware flash failed (exit {proc.returncode})")
            print(f"  FAIL: native firmware flash failed — run "
                  f"bazel run //cardputer_client:flash_firmware -- --port {port} for the full log")
            return
    except FileNotFoundError as exc:
        result.fail(f"flash.sh failed to run (bash? subprocess/docker): {exc}")
        print(f"  FAIL: {exc}")
        return
    except Exception as exc:
        result.fail(f"Native firmware flash error: {exc}")
        print(f"  FAIL: {exc}")
        return

    if inject_dest_hash:
        result.ok(f"Built + flashed native firmware on {port} (DEST_HASH baked)")
        print(f"  OK: native firmware flashed on {port}, DEST_HASH baked in")
    else:
        result.ok(f"Built + flashed native firmware on {port} (no DEST_HASH — announce-only)")
        print(f"  OK: native firmware flashed on {port} (no DEST_HASH — device will not send)")


def _inject_dest_hash(ser, client_root: str) -> str:
    """Patch DEST_HASH in the Cardputer's config.py to the server's hash.

    Loads (or creates) the persisted server identity on the host and
    derives its ``lxmf.delivery`` destination hash — the value the
    Cardputer needs to reach the server.  The source ``config.py`` is
    patched in memory (the source tree is never modified) and uploaded
    over the copy already on the device, then verified with a fresh
    import so a cached ``config`` module cannot mask a failed write.

    Returns the injected destination hash hex string.

    Raises on any failure — DEST_HASH is mandatory for the LoRa mesh to
    function; silently skipping it would leave a Cardputer that sends
    into the void.
    """
    dest_hash = ensure_delivery_destination_hash()

    config_path = os.path.join(client_root, "config.py")
    with open(config_path) as f:
        original_config = f.read()

    patched_config, n_subs = re.subn(
        r'DEST_HASH\s*=\s*(?:"[^"]*"|None)',
        f'DEST_HASH = "{dest_hash}"',
        original_config,
    )
    if n_subs != 1:
        raise RuntimeError(
            "Could not patch DEST_HASH in config.py — expected exactly one "
            "DEST_HASH assignment (string or None)."
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(patched_config)
            tmp_path = tmp.name
        uploaded = upload_file(ser, tmp_path, "config.py")
        if not uploaded:
            raise RuntimeError("Failed to upload patched config.py")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Verify the hash landed on the device (bypass the sys.modules cache
    # in case main.py already imported the stale config).
    from cardputer_client.flash import exec_raw

    ok, out = exec_raw(
        ser,
        "import sys\n"
        "if 'config' in sys.modules:\n"
        "    del sys.modules['config']\n"
        "import config\n"
        "print(config.DEST_HASH)\n",
    )
    if not ok or dest_hash not in out:
        raise RuntimeError(
            f"DEST_HASH verification failed — device config does not report "
            f"{dest_hash}: {out[:200]}"
        )

    return dest_hash


def _flash_cardputer_client(port: str, client_root: str, result: DeviceResult,
                            inject_dest_hash: bool = True) -> None:
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

        # Inject the server's delivery destination hash so the Cardputer
        # knows where to send (issue #70).  The identity is loaded from /
        # created at ~/.local/share/lmao_server/lxmf/identity — the same
        # file the server container loads via its volume mount, so both
        # sides are guaranteed to agree.
        if inject_dest_hash:
            print("  Injecting server DEST_HASH into config.py ...")
            try:
                dest_hash = _inject_dest_hash(ser, client_root)
                print(f"    DEST_HASH = {dest_hash}")
            except Exception as exc:
                result.fail(f"DEST_HASH injection failed: {exc}")
                print(f"  FAIL: DEST_HASH injection failed — {exc}")
                return

        # Ahead-of-time bytecode: UIFlow2's ~177 KB GC heap cannot hold the .py
        # sources' bytecode (the LXMF send path dies with "MemoryError
        # allocating 136 bytes"), so ship .mpy and drop the .py siblings that
        # would shadow it.  No-op when mpy-cross is unavailable.
        try:
            deploy_mpy_bytecode(ser, client_root)
        except Exception as exc:  # never fail the flash on the optimisation
            print(f"  WARNING: .mpy deployment skipped: {exc}")

        # MicroPython dependencies (the SX1262 driver + contextlib) are vendored
        # in cardputer_client/lib/ and uploaded above, so a flash needs no
        # network.  Only fall back to mip if the vendored driver is missing.
        if os.path.isfile(os.path.join(client_root, "lib", "lora", "sx126x.py")):
            print("  MicroPython dependencies: vendored in lib/ (no network needed)")
        else:
            print("  Installing MicroPython dependencies via mip ...")
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
    parser.add_argument(
        "--skip-dest-hash",
        action="store_true",
        help="Skip injecting the server DEST_HASH into the Cardputer config "
        "(e.g. when the server runs on a different host).",
    )
    cp_group = parser.add_mutually_exclusive_group()
    cp_group.add_argument(
        "--micropython-cardputer",
        action="store_true",
        help="Flash the MicroPython client to the Cardputer.  This is the "
        "default; the flag is kept for compatibility with existing scripts.",
    )
    cp_group.add_argument(
        "--native-cardputer",
        action="store_true",
        help="Flash the native C firmware (PR1 sensor node; no display/receive "
        "path) instead of the MicroPython client.  The native stack is "
        "reserved for tight-heap hardware (Sprout/Atom Lite native client), "
        "so it is no longer the Cardputer default.",
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
        if args.native_cardputer and not client_root:
            # The native path does not need the MicroPython client source tree.
            client_root = None
        port = find_cardputer_port(args.cardputer_port)
        if not port:
            cp_result.skip("No Cardputer detected on USB")
            print("Cardputer: SKIP — not detected on USB")
        elif args.native_cardputer:
            print("Cardputer: using native C firmware (--native-cardputer)")
            _flash_cardputer_native(
                port, cp_result, inject_dest_hash=not args.skip_dest_hash,
            )
        else:
            # MicroPython is the Cardputer's default runtime: its LoRa/MicroPython
            # libraries are the maintained, field-proven path for this board.  The
            # native C stack is reserved for tight-heap hardware (Sprout/Atom Lite).
            if not client_root:
                cp_result.fail("Cannot locate cardputer_client/ directory. Specify with --client-root.")
                print("Cardputer: FAIL — cannot locate cardputer_client/ directory")
            else:
                print("Cardputer: using MicroPython client (default)")
                _flash_cardputer_client(
                    port, client_root, cp_result,
                    inject_dest_hash=not args.skip_dest_hash,
                )

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
                    deploy_lmao_server(pi_result)
                else:
                    print("  Skipping K8s deploy — image build/release did not succeed")

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
