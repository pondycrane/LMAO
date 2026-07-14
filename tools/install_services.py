"""
Install server-side services: Docker build on the Pi and Kubernetes
manifest application to the cluster.

Provides three public functions that are called by install_all.py's main()
pipeline when the --include-services flag is set.

Usage (via Bazel):
    bazel run //tools:install_all -- --include-services

Prerequisites:
    - docker CLI installed and accessible on PATH
    - kubectl CLI installed and configured for a reachable cluster
    - Dockerfile at repo root
    - k8s/lmao-service.yaml and k8s/nats-server.yaml at repo root
"""

# Error handling convention:
#   - Functions that operate on Kubernetes resources fail fast (result.fail + return).
#   - `run_pi_server()` uses graceful degradation (WARNING + continue) so the
#     server container starts even if systemd install or container cleanup fails.

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

# Default NATS server for the LMAO server container (overridable via env).
_DEFAULT_NATS_SERVER = "nats://localhost:4222"

if TYPE_CHECKING:
    from tools.install_all import DeviceResult

# Default local Docker registry address (used when --setup-registry is set).
DEFAULT_REGISTRY_HOST = "192.168.0.36"
DEFAULT_REGISTRY_PORT = 5000

# ---------------------------------------------------------------------------
# Serial port detection — distinguish RNode from Cardputer
# ---------------------------------------------------------------------------


def _find_system_python() -> str | None:
    """Return a system Python that has ``rns`` installed, or None."""
    import shutil as _shutil

    for candidate in [
        _shutil.which("python3"),
        _shutil.which("python"),
        "/usr/bin/python3",
    ]:
        if candidate is None:
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "import RNS; print('ok')"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip() == "ok":
                return candidate
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def _probe_for_cardputer(port: str) -> bool:
    """Try entering MicroPython raw REPL on *port* to detect a Cardputer.

    Sends Ctrl+C (enter raw REPL), Ctrl+B (exit raw REPL). Returns True if
    the MicroPython banner is detected.
    """
    try:
        import serial as _serial

        ser = _serial.Serial(port, 115200, timeout=2)
        time.sleep(0.5)
        # Drain any garbage
        ser.reset_input_buffer()
        # Send Ctrl+C to interrupt, then Ctrl+A to enter raw REPL
        ser.write(b"\r\x03\x03")
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"\r\x01")
        time.sleep(0.3)
        banner = ser.read(256)
        ser.close()
        # Raw REPL banner contains "raw REPL" or "MicroPython"
        combined = banner.lower()
        return b"raw repl" in combined or b"micropython" in combined or b">==" in combined
    except Exception:
        return False


def _probe_for_rnode(port: str) -> bool:
    """Check whether *port* is running RNode firmware.

    Uses ``rnodeconf --info`` for definitive detection.  When ``rns``
    is not available, falls back to VID/PID heuristics via the caller.

    The ``rnodeconf --info`` command queries the RNode firmware version,
    frequency, and other parameters.  Non-RNode devices (Cardputer,
    plain serial adapters) will either time out or return non-zero.
    """
    system_python = _find_system_python()
    if system_python is None:
        print(f"  Probe[{port}]: no system Python with 'rns' found")
        return False

    try:
        # rnodeconf takes port as a positional argument, not --port
        result = subprocess.run(
            [system_python, "-m", "RNS.Utilities.rnodeconf", port, "--info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(f"  Probe[{port}]: rnodeconf --info timed out after 10s")
        return False
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  Probe[{port}]: rnodeconf failed: {exc}")
        return False

    if result.returncode != 0:
        stderr_tail = result.stderr.strip().split("\n")[-3:]
        stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
        print(f"  Probe[{port}]: rnodeconf exited {result.returncode}: {stderr_msg}")
        return False

    combined = (result.stdout + result.stderr).lower()
    is_rnode = "rnode firmware" in combined or "lora:" in combined
    if not is_rnode:
        print(f"  Probe[{port}]: rnodeconf output did not contain RNode signatures")
    return is_rnode


def detect_serial_devices() -> tuple[str | None, str | None]:
    """Detect and classify connected USB serial devices.

    Scans all serial ports, classifies each as RNode or Cardputer, and
    returns ``(rnode_port, cardputer_port)``.

    Detection strategy (in order of application):
      1. VID/PID lookup — classifies known USB serial adapters:
         - Espressif (0x303A)    → RNode (Heltec ESP32-LoRa)
         - CP210x (0x10C4)       → Cardputer (M5Stack)
         - CH340 (0x1A86)        → Cardputer (alternative USB-UART)
      2. ``rnodeconf --info`` probe — definitive check for unknown VID/PID.
         RNode firmware responds with frequency, bandwidth, and version.
      3. Path-based fallback — first existing port wins.

    Note: We do NOT use MicroPython raw REPL probing to distinguish the two
    because the RNode firmware itself is MicroPython-based and responds to
    the same REPL protocol as the Cardputer.

    Returns:
        ``(rnode_port, cardputer_port)`` where each is a device path or
        None if not found.
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        print("  WARNING: pyserial not available — falling back to path-based detection")
        rn = _detect_rnode_port_fallback()
        return rn, None

    ports = list(serial.tools.list_ports.comports())
    usb_ports = [
        p
        for p in ports
        if p.vid is not None and p.device.startswith("/dev/tty") and "ttyAMA" not in p.device
    ]

    if not usb_ports:
        print("  No USB serial devices found — falling back to path-based detection")
        rn = _detect_rnode_port_fallback()
        return rn, None

    candidates: list[tuple[str, int | None, int | None]] = []
    for p in usb_ports:
        candidates.append((p.device, p.vid, p.pid))

    print(f"  Found {len(candidates)} USB serial device(s)")
    for dev, vid, pid in candidates:
        vid_s = f"0x{vid:04X}" if vid else "???"
        pid_s = f"0x{pid:04X}" if pid else "???"
        print(f"    {dev}: {vid_s}:{pid_s}")

    # ---- Phase 1: identify RNode via VID/PID and rnodeconf validation ----
    rnode_port: str | None = None
    cardputer_port: str | None = None
    unclassified: list[str] = []

    # VID/PID lookup table — maps (vid, pid) to device role
    # Order matters: more specific matches first
    vid_pid_map: list[tuple[int, int, str]] = [
        # Espressif ESP32 native USB → Heltec RNode
        (0x303A, 0x4001, "rnode"),
        # RNode firmware changes PID to 0x1001
        (0x303A, 0x1001, "rnode"),
        # CP210x → M5Stack Cardputer (or generic USB-UART)
        (0x10C4, 0xEA60, "cardputer"),
        # CH340 → generic ESP32 (Cardputer or other)
        (0x1A86, 0x7523, "cardputer"),
    ]

    for dev, vid, pid in candidates:
        print(f"  Checking {dev}...", end="", flush=True)
        classified = False

        # Check VID/PID first
        for map_vid, map_pid, role in vid_pid_map:
            if vid == map_vid and pid == map_pid:
                print(f" {role} (VID:PID=0x{vid:04X}:0x{pid:04X})")
                if role == "rnode":
                    rnode_port = dev
                elif role == "cardputer":
                    cardputer_port = dev
                classified = True
                break

        if classified:
            continue

        # Unknown VID/PID — try rnodeconf probe as last resort
        if _probe_for_rnode(dev):
            print(" RNode (probe) ✓")
            rnode_port = dev
        else:
            print(" unknown — no matching VID/PID")
            unclassified.append(dev)

    # ---- Phase 2: report unclassified devices (do NOT blindly assign) ----
    if rnode_port is None and cardputer_port is None and unclassified:
        print(
            f"  WARNING: {len(unclassified)} unclassified device(s) found —"
            f" skipping automatic role assignment."
        )
        for dev in unclassified:
            print(f"    {dev} — no matching VID/PID and RNode probe failed/not available")

    if rnode_port:
        print(f"  ✓ RNode port: {rnode_port}")
    else:
        print("  ✗ No RNode port detected")

    if cardputer_port:
        print(f"  ✓ Cardputer port: {cardputer_port}")
    else:
        print("  ✗ No Cardputer port detected")

    return rnode_port, cardputer_port


def _detect_rnode_port_fallback() -> str | None:
    """Fallback: return the first common serial port that exists.

    Used when pyserial is not available for VID/PID probing.
    Also used when the main detection function falls through without
    a classification.
    """
    for port in ["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyACM1", "/dev/ttyUSB1"]:
        if os.path.exists(port):
            return port
    return None


def _run_kubectl_step(
    result: DeviceResult,
    step_name: str,
    cmd: list[str],
) -> subprocess.CompletedProcess | None:
    """Run a kubectl subcommand and handle errors consistently.

    On success, returns the ``CompletedProcess``.  On failure (non-zero
    return code, ``SubprocessError``, or unexpected exception), updates
    *result* to FAIL, prints diagnostics, and returns ``None``.

    The caller must check the return value and return early if ``None``.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        step_name: Human-readable name for the step (e.g. "apply").
        cmd: The command list to pass to ``subprocess.run``.

    Returns:
        ``CompletedProcess`` on success, ``None`` on failure.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"kubectl {step_name} failed: {stderr_msg}")
            print(f"  FAIL: kubectl {step_name} failed — {stderr_msg}")
            return None
        return proc
    except subprocess.SubprocessError as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"kubectl error ({step_name}): {exc}")
        print(f"  FAIL: {exc}")
        return None
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during kubectl {step_name}: {exc}")
        print(f"  FAIL: {exc}")
        return None


def _find_repo_root() -> str | None:
    """Walk up from this module's directory looking for the repo root.

    Identifies the repo root by locating a ``Dockerfile`` or ``.git``
    directory in an ancestor directory.

    When run via ``bazel run``, Bazel sets ``BUILD_WORKSPACE_DIRECTORY``
    to the actual workspace root — use that directly to avoid the Bazel
    sandbox execroot (which has no .git or real Dockerfile).

    Returns:
        Absolute path to the repo root, or ``None`` if not found.
    """
    # Bazel-run path: BUILD_WORKSPACE_DIRECTORY points to the real workspace root
    bw = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if bw and os.path.isdir(bw):
        print(f"  DEBUG: Found repo root via BUILD_WORKSPACE_DIRECTORY: {bw}")
        return bw

    current = os.path.dirname(os.path.abspath(__file__))
    print(f"  DEBUG: Searching for repo root, starting at {current}")
    for _ in range(10):
        if os.path.isfile(os.path.join(current, "Dockerfile")):
            print(f"  DEBUG: Found repo root via Dockerfile at {current}")
            return current
        if os.path.isdir(os.path.join(current, ".git")):
            print(f"  DEBUG: Found repo root via .git at {current}")
            return current
        print(f"  DEBUG: Checked {current}, no Dockerfile/.git found")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def install_pi_server(result: DeviceResult, repo_root: str | None = None) -> None:
    """Build the lmao-server Docker image via ``docker build``.

    Checks for the ``docker`` CLI on PATH; if not found, marks the
    result as SKIP with a diagnostic message.  Otherwise runs
    ``docker build -t lmao-server .`` from *repo_root*.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure it is set to FAIL.

    Note:
        This function builds the image only.  Starting the container
        is left to the operator (e.g. ``docker run lmao-server``).

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``Dockerfile``.
            When ``None``, auto-detected via ``_find_repo_root()``.
    """

    print("\n--- Pi Server: Docker build ---")

    if repo_root is None:
        repo_root = _find_repo_root()

    if not repo_root:
        result.fail("Cannot locate repo root (no Dockerfile found)")
        print("  FAIL: Cannot locate repo root (no Dockerfile found)")
        return

    if shutil.which("docker") is None:
        result.skip("Docker not found on PATH — install with: apt-get install docker.io")
        print("  SKIP: Docker not found on PATH")
        return

    try:
        proc = subprocess.run(
            ["docker", "build", "-t", "lmao-server", "."],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            result.ok("Docker image built (lmao-server:latest)")
            print("  OK: Docker image built (lmao-server:latest)")
        else:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"Docker build failed: {stderr_msg}")
            print(f"  FAIL: Docker build failed — {stderr_msg}")
    except subprocess.SubprocessError as exc:
        result.fail(f"Docker build error: {exc}")
        print(f"  FAIL: {exc}")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during Docker build: {exc}")
        print(f"  FAIL: {exc}")


def _apply_iot_ingest_manifest(
    result: DeviceResult,
    repo_root: str,
    registry_host: str,
    registry_port: int,
) -> None:
    """Apply the IoT ingest K8s manifest and configure the deployment
    to pull from a local Docker registry.

    Applies ``k8s/iot-ingest.yaml``, then sets the container image to
    ``{registry_host}:{registry_port}/lmao-iot-ingest:latest`` and
    patches ``imagePullPolicy`` to ``Always``.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``k8s/``.
        registry_host: Hostname or IP of the local Docker registry.
        registry_port: Port of the local Docker registry.
    """

    print(
        f"\n--- IoT Ingest Consumer: deploy from registry "
        f"({registry_host}:{registry_port}/lmao-iot-ingest:latest) ---"
    )

    if shutil.which("kubectl") is None:
        result.skip("kubectl not found on PATH — install with: apt-get install kubectl")
        print("  SKIP: kubectl not found on PATH")
        return

    manifest_path = os.path.join(repo_root, "k8s", "iot-ingest.yaml")
    if not os.path.isfile(manifest_path):
        result.fail(f"Manifest not found: {manifest_path}")
        print(f"  FAIL: Manifest not found: {manifest_path}")
        return

    _applied = False  # Track whether base manifest was applied

    # Step 1 — apply the base manifest (PVC, ConfigMap, Deployment)
    proc = _run_kubectl_step(result, "apply", ["kubectl", "apply", "-f", manifest_path])
    if proc is None:
        return
    _applied = True

    # Step 2 — set container image to registry reference
    registry_image = f"{registry_host}:{registry_port}/lmao-iot-ingest:latest"
    proc = _run_kubectl_step(
        result,
        "set image",
        [
            "kubectl",
            "set",
            "image",
            "deployment/iot-ingest-consumer",
            f"consumer={registry_image}",
        ],
    )
    if proc is None:
        if _applied:
            warning = (
                "  WARNING: k8s/iot-ingest.yaml was already applied. "
                "Manual rollback: kubectl delete -f k8s/iot-ingest.yaml"
            )
            print(warning)
            result.detail += " " + warning
        return
    print(f"  Image set to: {registry_image}")

    # Step 3 — patch imagePullPolicy to Always
    proc = _run_kubectl_step(
        result,
        "patch",
        [
            "kubectl",
            "patch",
            "deployment",
            "iot-ingest-consumer",
            "-p",
            '{"spec":{"template":{"spec":{"containers":[{"name":"consumer","imagePullPolicy":"Always"}]}}}}',
        ],
    )
    if proc is None:
        if _applied:
            warning = (
                "  WARNING: k8s/iot-ingest.yaml was already applied. "
                "Manual rollback: kubectl delete -f k8s/iot-ingest.yaml"
            )
            print(warning)
            result.detail += " " + warning
        return
    print("  imagePullPolicy patched to Always")

    result.ok(f"IoT Ingest Consumer deployed from registry ({registry_image})")
    print(f"  OK: IoT Ingest Consumer deployed from registry ({registry_image})")


def install_iot_ingest_consumer(
    result: DeviceResult,
    repo_root: str | None = None,
    registry_host: str | None = None,
    registry_port: int | None = None,
) -> None:
    """Build the iot-ingest Docker image and apply its K8s manifest.

    Builds ``Dockerfile.iot-ingest`` via ``docker build``, then applies
    ``k8s/iot-ingest.yaml`` via ``kubectl apply -f``.

    When *registry_host* and *registry_port* are both provided, the
    Docker build step is skipped and the deployment is configured to
    pull from the local registry at
    ``{registry_host}:{registry_port}/lmao-iot-ingest:latest`` instead.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure it is set to FAIL.  Missing prerequisites (Docker,
    kubectl) result in SKIP.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``Dockerfile.iot-ingest``
            and ``k8s/``.  When ``None``, auto-detected via ``_find_repo_root()``.
        registry_host: Hostname or IP of the local Docker registry.
            When provided together with *registry_port*, the Docker build
            step is skipped and the deployment pulls from the registry.
        registry_port: Port of the local Docker registry.
    """

    print("\n--- IoT Ingest Consumer: Docker build + K8s apply ---")

    if repo_root is None:
        repo_root = _find_repo_root()

    if not repo_root:
        result.fail("Cannot locate repo root (no Dockerfile found)")
        print("  FAIL: Cannot locate repo root (no Dockerfile found)")
        return

    # ── Registry path: skip docker build, use local registry ──
    if registry_host is not None and registry_port is not None:
        _apply_iot_ingest_manifest(result, repo_root, registry_host, registry_port)
        return

    # ── Docker build ───────────────────────────────────────────
    dockerfile = os.path.join(repo_root, "Dockerfile.iot-ingest")

    if shutil.which("docker") is None:
        result.skip("Docker not found on PATH — install with: apt-get install docker.io")
        print("  SKIP: Docker not found on PATH")
        return

    if not os.path.isfile(dockerfile):
        result.fail(f"Dockerfile not found: {dockerfile}")
        print(f"  FAIL: Dockerfile not found: {dockerfile}")
        return

    try:
        proc = subprocess.run(
            ["docker", "build", "-f", dockerfile, "-t", "lmao-iot-ingest", "."],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("  OK: Docker image built (lmao-iot-ingest:latest)")
        else:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"Docker build failed: {stderr_msg}")
            print(f"  FAIL: Docker build failed — {stderr_msg}")
            return
    except subprocess.SubprocessError as exc:
        result.fail(f"Docker build error: {exc}")
        print(f"  FAIL: {exc}")
        return
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during Docker build: {exc}")
        print(f"  FAIL: {exc}")
        return

    # ── kubectl apply ──────────────────────────────────────────
    if shutil.which("kubectl") is None:
        result.skip("kubectl not found on PATH — install with: apt-get install kubectl")
        print("  SKIP: kubectl not found on PATH")
        return

    manifest_path = os.path.join(repo_root, "k8s", "iot-ingest.yaml")
    if not os.path.isfile(manifest_path):
        result.fail(f"Manifest not found: {manifest_path}")
        print(f"  FAIL: Manifest not found: {manifest_path}")
        return

    try:
        proc = subprocess.run(
            ["kubectl", "apply", "-f", manifest_path],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"kubectl apply -f k8s/iot-ingest.yaml failed: {stderr_msg}")
            print(f"  FAIL: kubectl apply failed — {stderr_msg}")
            return

        result.ok("IoT Ingest Consumer deployed (Docker build + kubectl apply)")
        print("  OK: IoT Ingest Consumer deployed")
    except subprocess.SubprocessError as exc:
        result.fail(f"kubectl error: {exc}")
        print(f"  FAIL: {exc}")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during kubectl apply: {exc}")
        print(f"  FAIL: {exc}")


def install_k8s_services(result: DeviceResult, repo_root: str | None = None) -> None:
    """Apply Kubernetes manifests via ``kubectl apply -f``.

    Applies ``k8s/lmao-service.yaml`` and ``k8s/nats-server.yaml``.
    Checks for the ``kubectl`` CLI on PATH first; if not found, marks
    the result as SKIP.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure it is set to FAIL.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``k8s/``.
            When ``None``, auto-detected via ``_find_repo_root()``.
    """

    print("\n--- K8s Services: kubectl apply ---")

    if repo_root is None:
        repo_root = _find_repo_root()

    if not repo_root:
        result.fail("Cannot locate repo root (no Dockerfile found)")
        print("  FAIL: Cannot locate repo root (no Dockerfile found)")
        return

    if shutil.which("kubectl") is None:
        result.skip("kubectl not found on PATH — install with: apt-get install kubectl")
        print("  SKIP: kubectl not found on PATH")
        return

    manifests = [
        os.path.join("k8s", "lmao-service.yaml"),
        os.path.join("k8s", "nats-server.yaml"),
    ]

    applied: list[str] = []

    for manifest in manifests:
        manifest_path = os.path.join(repo_root, manifest)
        if not os.path.isfile(manifest_path):
            if applied:
                print(f"  WARNING: {', '.join(applied)} were already applied.")
                print(
                    "  Manual rollback: kubectl delete -f k8s/<manifest> for each applied manifest"
                )
            result.fail(f"Manifest not found: {manifest}")
            print(f"  FAIL: Manifest not found: {manifest}")
            return

        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", manifest_path],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                stderr_tail = proc.stderr.strip().split("\n")[-3:]
                stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
                fail_msg = f"kubectl apply -f {manifest} failed: {stderr_msg}"
                if applied:
                    fail_msg += (
                        f"  WARNING: {', '.join(applied)} were already applied."
                        f" Manual rollback: kubectl delete -f k8s/<manifest>"
                        f" for each applied manifest"
                    )
                result.fail(fail_msg)
                print(f"  FAIL: {fail_msg}")
                return
            applied.append(manifest)
        except subprocess.SubprocessError as exc:
            fail_msg = f"kubectl error ({manifest}): {exc}"
            if applied:
                fail_msg += (
                    f"  WARNING: {', '.join(applied)} were already applied."
                    f" Manual rollback: kubectl delete -f k8s/<manifest>"
                    f" for each applied manifest"
                )
            result.fail(fail_msg)
            print(f"  FAIL: {exc}")
            return
        except Exception as exc:
            import traceback

            traceback.print_exc()
            fail_msg = f"Unexpected error during kubectl apply ({manifest}): {exc}"
            if applied:
                fail_msg += (
                    f"  WARNING: {', '.join(applied)} were already applied."
                    f" Manual rollback: kubectl delete -f k8s/<manifest>"
                    f" for each applied manifest"
                )
            result.fail(fail_msg)
            print(f"  FAIL: {exc}")
            return

    manifests_str = ", ".join(applied)
    result.ok(f"Applied {manifests_str}")
    print(f"  OK: Applied {manifests_str}")


def setup_registry(result: DeviceResult, repo_root: str | None = None) -> None:
    """Start the local Docker registry and push all LMAO images.

    Delegates to ``docker/registry/manage.sh`` which wraps docker-compose
    and docker push under the hood.  Checks for the ``docker`` CLI and the
    manage script before proceeding.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure it is set to FAIL.  Missing ``docker`` CLI on PATH
    results in SKIP; missing ``manage.sh`` or repo root results in FAIL.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``docker/registry/``.
            When ``None``, auto-detected via ``_find_repo_root()``.
    """

    print("\n--- Local Docker Registry: start + push ---")

    if repo_root is None:
        repo_root = _find_repo_root()

    if not repo_root:
        result.fail("Cannot locate repo root (no Dockerfile found)")
        print("  FAIL: Cannot locate repo root (no Dockerfile found)")
        return

    manage_script = os.path.join(repo_root, "docker", "registry", "manage.sh")

    if shutil.which("docker") is None:
        result.skip("Docker not found on PATH")
        print("  SKIP: Docker not found on PATH")
        return

    if not os.path.isfile(manage_script):
        result.fail(f"Registry manage script not found: {manage_script}")
        print(f"  FAIL: manage.sh not found at {manage_script}")
        return

    try:
        # Step 1 -- start the registry container
        print("  Starting registry container...")
        proc = subprocess.run(
            [manage_script, "start"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"Registry start failed: {err_msg}")
            print(f"  FAIL: Registry start failed -- {err_msg}")
            return

        print("  Registry container started.")

        # Step 2 -- push all LMAO images
        print("  Building and pushing LMAO images...")
        env = os.environ.copy()
        proc = subprocess.run(
            [manage_script, "push"],
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"Registry push failed: {err_msg}")
            print(f"  FAIL: Registry push failed -- {err_msg}")
            return

        result.ok("Registry started and LMAO images pushed")
        print("  OK: Registry running, images pushed")

    except subprocess.SubprocessError as exc:
        result.fail(f"Registry setup error: {exc}")
        print(f"  FAIL: {exc}")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during registry setup: {exc}")
        print(f"  FAIL: {exc}")


def _resolve_nats_address() -> str | None:
    """Resolve a reachable NATS server address from the K8s cluster.

    Tries, in order:
    1. If the NATS service is ``NodePort``, returns the first node's
       IP with the assigned NodePort (reachable from outside the
       cluster network).
    2. If the service is ``ClusterIP``, returns the ClusterIP:4222
       (reachable only from within the cluster).
    3. Falls back to ``None`` if no reachable address could be
       determined (kubectl unavailable, service not found, unsupported
       service type, or resolution failure).
    """
    if shutil.which("kubectl") is None:
        return None
    try:
        # Print current kubectl context for diagnostics
        ctx_proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ctx_proc.returncode == 0 and ctx_proc.stdout.strip():
            print(f"  Using kubectl context: {ctx_proc.stdout.strip()}")

        # Step 1: get service type and ClusterIP
        svc_proc = subprocess.run(
            [
                "kubectl",
                "get",
                "svc",
                "nats-server",
                "-n",
                "default",
                "-o",
                "jsonpath={.spec.type}|{.spec.clusterIP}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if svc_proc.returncode != 0:
            return None
        parts = svc_proc.stdout.strip().split("|")
        svc_type = parts[0] if len(parts) > 0 else ""
        cluster_ip = parts[1] if len(parts) > 1 else ""

        # Step 2: NodePort path — find a node IP + NodePort
        if svc_type == "NodePort":
            port_proc = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "svc",
                    "nats-server",
                    "-n",
                    "default",
                    "-o",
                    "jsonpath={.spec.ports[0].nodePort}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if port_proc.returncode == 0:
                node_port = port_proc.stdout.strip()
                if node_port:
                    # Get first ready node's InternalIP
                    node_proc = subprocess.run(
                        [
                            "kubectl",
                            "get",
                            "nodes",
                            "-o",
                            "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if node_proc.returncode == 0:
                        node_ip = (
                            node_proc.stdout.strip().split()[0] if node_proc.stdout.strip() else ""
                        )
                        if node_ip:
                            return f"nats://{node_ip}:{node_port}"

        # Step 3: ClusterIP path
        if svc_type == "ClusterIP" and cluster_ip and cluster_ip != "None":
            print(
                "  WARNING: NATS ClusterIP resolved \u2014 this address is only reachable\n"
                "           from inside the K8s cluster. If the container fails to\n"
                "           connect, set NATS_SERVER=nats://<external-addr>:4222"
            )
            return f"nats://{cluster_ip}:4222"

    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  WARNING: NATS address resolution failed \u2014 {exc}")
        return None
    return None


def _detect_rnode_port() -> str | None:
    """Auto-detect the RNode serial port.

    Priority:
    1. ``LMAO_RNODE_PORT`` environment variable (explicit override).
    2. ``detect_serial_devices()`` — VID/PID + firmware probing.
    3. Fallback: first existing common serial port.
    4. Hardcoded default: ``/dev/ttyUSB0`` when all else fails.

    Returns:
        Device path (e.g. ``/dev/ttyUSB0``) or ``None`` if no port found.
    """
    env_port = os.environ.get("LMAO_RNODE_PORT")
    if env_port:
        # Even with an explicit env override, warn if the device doesn't exist
        if not os.path.exists(env_port):
            print(f"  WARNING: LMAO_RNODE_PORT={env_port} set but device not found!")
        return env_port

    rnode, _ = detect_serial_devices()
    if rnode:
        return rnode

    fallback = _detect_rnode_port_fallback()
    if fallback:
        print(f"  WARNING: No RNode identified by probing — using first available port {fallback}")
        return fallback

    print("  WARNING: No serial port found — container will start without LoRa device")
    return None


def _docker_psql(filter_expr: str) -> str | None:
    """Return the container ID matching a Docker filter, or None."""
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", filter_expr],
        capture_output=True,
        text=True,
        timeout=15,
    )
    cid = result.stdout.strip()
    return cid if cid else None


def run_pi_server(result: DeviceResult, repo_root: str | None = None) -> None:
    """Run the lmao-server Docker container and install a systemd service.

    Stops any existing ``lmao-server`` container, starts a new one with
    ``--network host`` and the detected RNode device passthrough, and
    creates a systemd unit at ``/etc/systemd/system/lmao-server.service``
    so the container starts on boot.

    The ``NATS_SERVER`` environment variable is passed through so the
    server can publish to the in-cluster NATS JetStream.  If unset, the
    script auto-discovers a reachable NATS address by querying
    ``kubectl``:

    1. If the service type is ``NodePort``, resolves the first node's
       IP with the assigned NodePort.
    2. If the service type is ``ClusterIP``, resolves the ClusterIP on
       port 4222.
    3. Otherwise, falls back to ``nats://localhost:4222``.

    The RNode device path follows the same detection as the server config
    (``LMAO_RNODE_PORT`` env var, then auto-detect).

    Requires root privileges (via ``sudo``) for systemd setup.

    The caller must pass a ``DeviceResult`` instance as *result*.  On full
    success the result is set to OK; on complete failure (e.g. missing
    Docker CLI, container exited after start) it is set to FAIL or SKIP.

    Note on persistence:
        The systemd unit is installed *before* starting the container so
        that the service survives reboot even if the immediate ``docker
        run`` fails.  If systemd installation fails (e.g., missing sudo
        access), the container is still started directly; only auto-start
        on boot is lost.

    Note:
        Errors in intermediate steps (stopping an existing container,
        installing the systemd unit) are treated as non-fatal warnings.
        The function continues to start the container even when those
        steps fail, so callers should check ``result`` for the final
        verdict rather than assuming a single error aborts the function.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root (unused, kept for interface
            consistency with other install_services functions).
    """
    _ = repo_root  # Kept for interface consistency; not needed here.

    print("\n--- Pi Server: Docker run + systemd ---")

    if shutil.which("docker") is None:
        result.skip("Docker not found on PATH")
        print("  SKIP: Docker not found on PATH")
        return

    # ── Detect RNode port ──
    rnode_port = _detect_rnode_port()
    rdevice_exists = rnode_port is not None and os.path.exists(rnode_port)
    if rdevice_exists:
        print(f"  RNode detected at: {rnode_port}")
    elif rnode_port:
        print(f"  RNode port {rnode_port} not found \u2014 container will start without LoRa.")
    else:
        print("  No RNode port detected \u2014 container will start without LoRa.")

    # ── Resolve NATS_SERVER ──
    nats_server = os.environ.get("NATS_SERVER")
    if nats_server is None:
        resolved = _resolve_nats_address()
        if resolved:
            nats_server = resolved
            print(f"  Resolved in-cluster NATS at {nats_server}")
        else:
            nats_server = _DEFAULT_NATS_SERVER
            print(f"  No in-cluster NATS found, using default {nats_server}")

    # ── Stop any existing lmao-server container ──
    print("  Stopping existing lmao-server container (if any)...")
    try:
        existing = _docker_psql("name=lmao-server")
    except subprocess.SubprocessError as exc:
        print(f"  WARNING: docker ps failed \u2014 {exc}")
        existing = None
    if existing:
        try:
            subprocess.run(
                ["docker", "stop", "lmao-server"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                ["docker", "rm", "lmao-server"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            print("  Stopped and removed existing container.")
        except subprocess.SubprocessError as exc:
            print(f"  WARNING: could not stop existing container \u2014 {exc}")
            print("  Attempting force-remove fallback...")
            try:
                subprocess.run(
                    ["docker", "rm", "-f", "lmao-server"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                print("  Force-removed existing container.")
            except subprocess.SubprocessError:
                print("  WARNING: force-remove also failed; container name may conflict")

    # ── Build ExecStart args (shared by docker run and systemd) ──
    exec_args = [
        "docker",
        "run",
        "--rm",
        "--name",
        "lmao-server",
        "--network",
        "host",
        "-e",
        f"NATS_SERVER={nats_server}",
        "-e",
        f"LMAO_RNODE_PORT={rnode_port}",
    ]
    if rdevice_exists:
        exec_args.extend(["--device", f"{rnode_port}:{rnode_port}"])
    exec_args.append("lmao-server:latest")

    # ── Install systemd service FIRST (always \u2014 persistence mechanism) ──
    service_unit = """[Unit]
Description=LMAO Server \u2014 Reticulum/LXMF LoRa mesh gateway
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker stop lmao-server
ExecStartPre=-/usr/bin/docker rm lmao-server
ExecStart={}
ExecStop=/usr/bin/docker stop lmao-server
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""".format(" ".join(exec_args))

    systemd_ok = False
    try:
        import tempfile

        fd, tmp_path = tempfile.mkstemp(prefix="lmao-server-", suffix=".service")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(service_unit)
            subprocess.run(
                ["sudo", "mv", tmp_path, "/etc/systemd/system/lmao-server.service"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                ["sudo", "systemctl", "enable", "lmao-server"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        print("  systemd service installed at /etc/systemd/system/lmao-server.service")
        print("  Enabled for auto-start on boot.")
        print()
        print("  Manage with:")
        print("    sudo systemctl start lmao-server    # Start now")
        print("    sudo systemctl stop lmao-server     # Stop")
        print("    sudo systemctl status lmao-server   # Check status")
        print("    sudo journalctl -u lmao-server -f   # Tail logs")
        systemd_ok = True
    except subprocess.SubprocessError as exc:
        stderr_hint = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            stderr_tail = stderr.strip().split("\n")[-3:]
            stderr_hint = ": " + "; ".join(stderr_tail)
        elif isinstance(exc, subprocess.TimeoutExpired) and exc.stderr:
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            stderr_hint = ": " + stderr.strip()
        print(f"  WARNING: systemd install failed{stderr_hint}")
        print("  (Server will run now but won't auto-start on boot \u2014 fix sudo access)")
    except PermissionError:
        print("  WARNING: systemd install requires sudo \u2014 skipping")
        print("  (Server will run now but won't auto-start on boot)")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"  WARNING: systemd install error: {exc}")

    # ── Start container (now, so it runs immediately) ──
    run_args = list(exec_args)
    # Change --rm to -d --restart unless-stopped for immediate run
    rm_idx = run_args.index("--rm")
    run_args[rm_idx] = "-d"
    restart_idx = run_args.index("--name")
    run_args.insert(restart_idx, "unless-stopped")
    run_args.insert(restart_idx, "--restart")

    print("  Starting container: {}".format(" ".join(run_args)))

    container_id = None
    try:
        proc = subprocess.run(run_args, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            container_id = proc.stdout.strip()[:12]
            print(f"  Container started: {container_id}")
        else:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            print(f"  WARNING: docker run failed \u2014 {err_msg}")
            print("  Systemd service is installed. Fix the issue then:")
            print("    sudo systemctl start lmao-server")
    except subprocess.SubprocessError as exc:
        print(f"  WARNING: docker run error \u2014 {exc}")
        print("  Systemd service is installed. Fix the issue then:")
        print("    sudo systemctl start lmao-server")

    # ── Verify container is running ──
    if container_id:
        print("  Verifying container...")
        try:
            proc = subprocess.run(
                ["docker", "ps", "--filter", "name=lmao-server", "--format", "{{.Status}}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            status = proc.stdout.strip()
            if status:
                print(f"  Container status: {status}")
                if systemd_ok:
                    result.ok(f"Container running + systemd: {container_id}")
                    print(f"  OK: lmao-server running ({container_id})")
                else:
                    result.ok(
                        f"Container running: {container_id} (systemd not installed \u2014 "
                        "will not survive reboot)"
                    )
                    print(f"  OK: lmao-server running ({container_id})")
                    print(
                        "  NOTE: systemd was not installed; container will not auto-start on boot."
                    )
            else:
                result.fail("Container exited after start")
                print("  FAIL: Container exited \u2014 check `docker logs lmao-server`")
        except subprocess.SubprocessError:
            result.fail("Container status check failed \u2014 verify manually: docker ps")
            print("  FAIL: Could not verify container status \u2014 docker ps failed")
    else:
        if systemd_ok:
            result.ok("Systemd service installed (container will start on boot or via systemctl)")
        else:
            result.fail("Container did not start and systemd was not installed")
