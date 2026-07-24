"""
Install server-side services: Docker build on the Pi and Kubernetes
manifest application to the cluster.

Provides three public functions that are called by install_all.py's main()
pipeline when the --include-services flag is set.

Usage (via Bazel):
    bazel run //tools:install_all -- --include-services

Release flow (internal services):
    All internal services are released through the local Docker registry
    (default ``192.168.0.36:5000``) and deployed via Docker from the
    registry image — a single, consistent release path:

    - Pi server: build → push ``lmao-server`` → ``docker pull`` +
      ``docker run`` the registry image (plus a systemd unit for
      auto-start on boot).
    - IoT ingest consumer: build → push ``lmao-iot-ingest`` →
      ``kubectl apply`` (the manifest references the registry image) →
      wait for the rollout and verify a pod is Running.

Prerequisites:
    - docker CLI installed and accessible on PATH
    - kubectl CLI installed and configured for a reachable cluster
    - local Docker registry running (``--setup-registry`` or
      ``docker/registry/manage.sh start``)
    - Dockerfile and Dockerfile.iot-ingest at repo root
    - k8s/lmao-service.yaml, k8s/nats-server.yaml, k8s/iot-ingest.yaml
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

    Delegates to :func:`lma_core.device_detect.probe_cardputer_repl`.
    """
    try:
        from lma_core.device_detect import probe_cardputer_repl

        return probe_cardputer_repl(port)
    except ImportError:
        pass

    # Fallback (pyserial must be available)
    try:
        import serial as _serial

        ser = _serial.Serial(port, 115200, timeout=2)
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.write(b"\r\x03\x03")
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"\r\x01")
        time.sleep(0.3)
        banner = ser.read(256)
        ser.close()
        combined = banner.lower()
        return b"raw repl" in combined or b"micropython" in combined or b">==" in combined
    except Exception:
        return False


def _probe_for_rnode(port: str) -> bool:
    """Check whether *port* is running RNode firmware.

    Delegates to :func:`lma_core.device_detect.probe_rnode`.
    """
    try:
        from lma_core.device_detect import probe_rnode

        return probe_rnode(port)
    except ImportError:
        # Fallback: inline probe
        import serial as _serial

        try:
            ser = _serial.Serial(port, 115200, timeout=2)
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.write(bytes([0xC0, 0x08, 0x73, 0xC0]))
            time.sleep(0.5)
            data = ser.read(100)
            ser.close()

            if not data:
                return False

            return (
                len(data) >= 4
                and data[0:1] == b"\xC0"
                and data[1] == 0x08
                and data[2] == 0x46
            )
        except Exception:
            return False


def detect_serial_devices() -> tuple[str | None, str | None]:
    """Detect and classify connected USB serial devices.

    Delegates to :func:`lma_core.device_detect.detect_devices`, which
    uses VID/PID + product strings from verified real-device fingerprints.
    No broad keyword fallback matching is performed.

    Returns:
        ``(rnode_port, cardputer_port)`` where each is a device path or
        ``None`` if not found.
    """
    try:
        from lma_core.device_detect import detect_devices

        result = detect_devices()
        rnode_port = result.rnode_port
        cardputer_port = result.cardputer_port

        # Print detection summary
        for info in result.all_ports:
            vid_s = f"0x{info.vid:04X}" if info.vid else "???"
            pid_s = f"0x{info.pid:04X}" if info.pid else "???"
            print(f"    {info.port}: {vid_s}:{pid_s}")

        if rnode_port:
            conf = result.confidence.get("rnode", "unknown")
            print(f"  ✓ RNode port: {rnode_port} (confidence: {conf})")
        else:
            print("  ✗ No RNode port detected")

        if cardputer_port:
            conf = result.confidence.get("cardputer", "unknown")
            print(f"  ✓ Cardputer port: {cardputer_port} (confidence: {conf})")
        else:
            print("  ✗ No Cardputer port detected")

        return rnode_port, cardputer_port
    except ImportError:
        print("  WARNING: lma_core.device_detect not available — falling back")
        rn = _detect_rnode_port_fallback()
        return rn, None


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


def _check_registry(
    host: str = DEFAULT_REGISTRY_HOST,
    port: int = DEFAULT_REGISTRY_PORT,
) -> tuple[bool, str]:
    """Check whether the local Docker registry API is reachable.

    Performs an HTTP GET against ``http://{host}:{port}/v2/`` (the
    registry API base, which returns 200 when healthy).

    Returns ``(ok, message)`` where *ok* is True when the registry
    responded with HTTP 200.  *message* contains diagnostic info or
    recovery instructions when the registry is unreachable.
    """
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/v2/"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200:
                return True, f"registry reachable at {host}:{port}"
            return False, f"registry at {host}:{port} returned HTTP {resp.status}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, (
            f"local Docker registry unreachable at {host}:{port} ({reason}) — "
            "start it with --setup-registry or ./docker/registry/manage.sh start"
        )


def _tag_and_push(result: DeviceResult, local_tag: str, registry_image: str) -> bool:
    """Tag *local_tag* as *registry_image* and push to the local registry.

    Returns True on success.  On failure updates *result* to FAIL,
    prints diagnostics, and returns False (caller must return early).
    """
    try:
        tag_proc = subprocess.run(
            ["docker", "tag", local_tag, registry_image],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if tag_proc.returncode != 0:
            err = tag_proc.stderr.strip() or "unknown error"
            result.fail(f"docker tag failed: {err}")
            print(f"  FAIL: docker tag failed — {err}")
            return False
        print(f"  Pushing {registry_image} ...")
        push_proc = subprocess.run(
            ["docker", "push", registry_image],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if push_proc.returncode != 0:
            stderr_tail = push_proc.stderr.strip().split("\n")[-3:]
            err = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"docker push {registry_image} failed: {err}")
            print(f"  FAIL: docker push failed — {err}")
            return False
        print(f"  OK: pushed {registry_image}")
        return True
    except subprocess.SubprocessError as exc:
        result.fail(f"docker push error: {exc}")
        print(f"  FAIL: {exc}")
        return False
    except Exception as exc:
        import traceback

        traceback.print_exc()
        result.fail(f"Unexpected error during docker push: {exc}")
        print(f"  FAIL: {exc}")
        return False


def install_pi_server(result: DeviceResult, repo_root: str | None = None) -> None:
    """Build the lmao-server image and release it via the local registry.

    Runs ``docker build -t lmao-server:latest .`` from *repo_root*, then
    tags and pushes the image to the local Docker registry
    (``{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}/lmao-server:latest``).
    Internal services are always released through the local registry so
    every deploy (Pi container, K8s pods) uses the same image source.

    Checks for the ``docker`` CLI on PATH; if not found, marks the
    result as SKIP with a diagnostic message.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure (build failure, unreachable registry, or push
    failure) it is set to FAIL.

    Note:
        This function builds and pushes the image only.  Deploying the
        container is handled by :func:`run_pi_server`, which pulls and
        runs the registry image via Docker.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``Dockerfile``.
            When ``None``, auto-detected via ``_find_repo_root()``.
    """

    print("\n--- Pi Server: Docker build + push to local registry ---")

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

    registry_image = f"{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}/lmao-server:latest"

    # ── Build ──
    try:
        proc = subprocess.run(
            ["docker", "build", "-t", "lmao-server:latest", "."],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            stderr_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"Docker build failed: {stderr_msg}")
            print(f"  FAIL: Docker build failed — {stderr_msg}")
            return
        print("  OK: Docker image built (lmao-server:latest)")
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

    # ── Release via the local Docker registry ──
    registry_ok, registry_msg = _check_registry()
    if not registry_ok:
        result.fail(f"Cannot release image: {registry_msg}")
        print(f"  FAIL: {registry_msg}")
        return

    if not _tag_and_push(result, "lmao-server:latest", registry_image):
        return

    result.ok(f"Image released to local registry ({registry_image})")
    print(f"  OK: {registry_image} released")


def _check_k8s_cluster() -> tuple[bool, str]:
    """Check if the K8s API server is reachable via kubectl.

    Returns ``(ok, message)`` where *ok* is True when the cluster is
    healthy (API server responds, at least one node ready), and False
    otherwise.  *message* contains diagnostic info or error details.
    """
    if shutil.which("kubectl") is None:
        return False, "kubectl not found on PATH"

    try:
        # Quick check: can we reach the API server?
        proc = subprocess.run(
            ["kubectl", "version", "--output=json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # A reachable API server shows up as "serverVersion" in the JSON
        # output — this is authoritative.  stderr is NOT a reliable error
        # indicator: kubectl prints benign warnings there (e.g. client/server
        # minor version skew) even on success.
        server_reachable = False
        if proc.returncode == 0 and proc.stdout:
            try:
                import json

                server_reachable = "serverVersion" in json.loads(proc.stdout)
            except ValueError:
                server_reachable = False

        if not server_reachable:
            stderr = (proc.stderr or "").strip() + (proc.stdout or "").strip()
            # kubectl --output=json was added in 1.28; fallback for older versions
            if "unknown flag" in stderr.lower():
                fallback = subprocess.run(
                    ["kubectl", "version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if fallback.returncode == 0 and "Server Version" in (fallback.stdout or ""):
                    server_reachable = True
                else:
                    err = ((fallback.stderr or "") + (fallback.stdout or "")).strip().lower()
                    if "connection refused" in err or "was refused" in err:
                        return False, (
                            "K8s API server is reachable but refusing connections — "
                            "the control-plane node may be starting up or stopped"
                        )
                    if "no route to host" in err or "i/o timeout" in err:
                        return False, (
                            "K8s API server is unreachable — "
                            "the control-plane node (192.168.0.45) may be powered off"
                        )
                    return False, f"kubectl version failed: {(fallback.stderr or '')[:200]}"
            else:
                err = stderr.lower()
                if "connection refused" in err or "was refused" in err:
                    return False, (
                        "K8s API server is reachable but refusing connections — "
                        "the control-plane node may be starting up or stopped"
                    )
                if "no route to host" in err or "i/o timeout" in err:
                    return False, (
                        "K8s API server is unreachable — "
                        "the control-plane node (192.168.0.45) may be powered off"
                    )
                return False, f"kubectl version failed: {(proc.stderr or '')[:200]}"

        # Check that at least one node is Ready
        proc = subprocess.run(
            [
                "kubectl",
                "get",
                "nodes",
                "-o",
                "jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            statuses = proc.stdout.strip().split()
            ready_count = sum(1 for s in statuses if s == "True")
            total = len(statuses)
            if ready_count == 0:
                return False, f"K8s cluster reachable but {total} node(s) not Ready"
            return True, f"K8s cluster healthy ({ready_count}/{total} nodes ready)"

        return True, "K8s API server reachable"

    except subprocess.TimeoutExpired:
        return False, "kubectl command timed out — cluster may be unreachable"
    except subprocess.SubprocessError as exc:
        return False, f"kubectl error: {exc}"


def install_iot_ingest_consumer(result: DeviceResult, repo_root: str | None = None) -> None:
    """Build, release, and deploy the IoT ingest consumer.

    Consistent release flow for internal services:

    1. Build ``lmao-iot-ingest:latest`` from ``Dockerfile.iot-ingest``.
    2. Tag and push to the local Docker registry
       (``{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}``) — the single
       source of truth for internal service images.
    3. Apply ``k8s/iot-ingest.yaml`` (which references the registry
       image directly).
    4. Wait for the Deployment rollout and verify a pod is Running, so
       a broken deploy is reported ``[FAIL]`` instead of silently
       succeeding.

    **K8s cluster check:** Before applying the manifest, the function
    checks whether the K8s API server is reachable. If the cluster is
    unreachable (e.g. control-plane node powered off), the Docker image
    is still built and pushed to the local registry so it is ready to
    deploy when the cluster recovers.  Clear recovery steps are printed
    and the stage is reported OK.

    The caller must pass a ``DeviceResult`` instance (imported lazily
    from ``install_all``) as *result*.  On success the result is set to
    OK; on failure (build, registry push, apply, or rollout) it is set
    to FAIL.  Missing prerequisites (Docker, kubectl) result in SKIP.

    Args:
        result: A ``DeviceResult`` instance (from ``tools.install_all``).
        repo_root: Path to the repository root containing ``Dockerfile.iot-ingest``
            and ``k8s/``.  When ``None``, auto-detected via ``_find_repo_root()``.
    """

    print("\n--- IoT Ingest Consumer: build + push + K8s deploy ---")

    if repo_root is None:
        repo_root = _find_repo_root()

    if not repo_root:
        result.fail("Cannot locate repo root (no Dockerfile found)")
        print("  FAIL: Cannot locate repo root (no Dockerfile found)")
        return

    # ── Check K8s cluster + local registry health early ──
    cluster_ok, cluster_msg = _check_k8s_cluster()
    print(f"  K8s cluster: {cluster_msg}")
    registry_ok, registry_msg = _check_registry()
    print(f"  Local registry: {registry_msg}")

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

    # Always build the Docker image (available for registry push or later use)
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

    # ── Release via the local Docker registry ──────────────────
    registry_image = f"{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}/lmao-iot-ingest:latest"
    if not registry_ok:
        result.fail(f"Cannot release image: {registry_msg}")
        print(f"  FAIL: {registry_msg}")
        return
    if not _tag_and_push(result, "lmao-iot-ingest:latest", registry_image):
        return

    # ── kubectl apply ──────────────────────────────────────────
    if not cluster_ok:
        print()
        print(f"  ╔══ K8s cluster unreachable ═══════════════════════════════╗")
        print(f"  ║  {cluster_msg:<56s}║")
        print(f"  ║                                                       ║")
        print(f"  ║  Docker image is built and pushed to the local        ║")
        print(f"  ║  registry.  When the cluster is back online, deploy   ║")
        print(f"  ║  with:                                                ║")
        print(f"  ║                                                       ║")
        print(f"  ║    kubectl apply -f k8s/iot-ingest.yaml                ║")
        print(f"  ║                                                       ║")
        print(f"  ║  To rebuild and push the image:                       ║")
        print(f"  ║    docker build -f Dockerfile.iot-ingest               ║")
        print(f"  ║      -t {registry_image} .                ║")
        print(f"  ║    docker push {registry_image}                    ║")
        print(f"  ╚═══════════════════════════════════════════════════════╝")
        print()

        result.ok(
            f"Docker image built and pushed to {registry_image}. "
            "K8s cluster unreachable — run 'kubectl apply -f k8s/iot-ingest.yaml' "
            "when the cluster is back online."
        )
        return

    if shutil.which("kubectl") is None:
        result.skip("kubectl not found on PATH — install with: apt-get install kubectl")
        print("  SKIP: kubectl not found on PATH")
        return

    manifest_path = os.path.join(repo_root, "k8s", "iot-ingest.yaml")
    if not os.path.isfile(manifest_path):
        result.fail(f"Manifest not found: {manifest_path}")
        print(f"  FAIL: Manifest not found: {manifest_path}")
        return

    # ── Deploy from the registry image ──
    proc = _run_kubectl_step(result, "apply", ["kubectl", "apply", "-f", manifest_path])
    if proc is None:
        return

    # Wait for the rollout so a broken deploy is reported [FAIL]
    # instead of silently succeeding.
    proc = _run_kubectl_step(
        result,
        "rollout status",
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/iot-ingest-consumer",
            "--timeout=180s",
        ],
    )
    if proc is None:
        print(
            "  Rollout did not complete — check: "
            "kubectl describe deployment iot-ingest-consumer"
        )
        return

    # Verify at least one pod is Running.
    proc = _run_kubectl_step(
        result,
        "get pods",
        [
            "kubectl",
            "get",
            "pods",
            "-l",
            "app=iot-ingest-consumer",
            "-o",
            "jsonpath={.items[*].status.phase}",
        ],
    )
    if proc is None:
        return
    phases = proc.stdout.split()
    if "Running" not in phases:
        result.fail(f"No iot-ingest-consumer pod Running (phases: {phases})")
        print(f"  FAIL: no iot-ingest-consumer pod Running (phases: {phases})")
        return

    result.ok(f"IoT Ingest Consumer deployed and Running ({registry_image})")
    print(f"  OK: IoT Ingest Consumer Running ({registry_image})")


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

    # ── Check K8s cluster health ──
    cluster_ok, cluster_msg = _check_k8s_cluster()
    print(f"  K8s cluster: {cluster_msg}")
    if not cluster_ok:
        print()
        print(f"  ╔══ K8s cluster unreachable ═══════════════════════════════╗")
        print(f"  ║  {cluster_msg:<56s}║")
        print(f"  ║                                                       ║")
        print(f"  ║  Fix the cluster connectivity then re-run:             ║")
        print(f"  ║    bazel run //tools:install_all -- --include-services  ║")
        print(f"  ╚═══════════════════════════════════════════════════════╝")
        print()
        result.fail(cluster_msg)
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

    # Don't bother if the cluster is unreachable
    cluster_ok, _cluster_msg = _check_k8s_cluster()
    if not cluster_ok:
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


def _docker_psql(filter_expr: str, all: bool = False) -> str | None:
    """Return the container ID matching a Docker filter, or None.

    When *all* is True, stopped containers are included (``docker ps -aq``).
    """
    result = subprocess.run(
        ["docker", "ps", "-aq" if all else "-q", "--filter", filter_expr],
        capture_output=True,
        text=True,
        timeout=15,
    )
    cid = result.stdout.strip()
    return cid if cid else None


def stop_pi_server_container() -> bool:
    """Best-effort stop of a running ``lmao-server`` container.

    Used before hardware probing/flashing so the server does not hold
    the RNode serial port — a running server races the RNode DETECT
    probe (async LoRa KISS frames interleave with the probe response).

    Never fails the pipeline: returns True when a container was found
    and stopped, False otherwise (no docker, no container, or error).
    """
    if shutil.which("docker") is None:
        return False
    try:
        if not _docker_psql("name=lmao-server", all=True):
            return False
        print("  Stopping lmao-server container (redeployed by the services stage) ...")
        subprocess.run(
            ["docker", "stop", "lmao-server"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Remove as well so the container name is free for the redeploy.
        subprocess.run(
            ["docker", "rm", "lmao-server"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return True
    except subprocess.SubprocessError:
        return False


def run_pi_server(result: DeviceResult, repo_root: str | None = None) -> None:
    """Deploy the lmao-server container from the local registry + systemd.

    Pulls ``{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}/lmao-server:latest``
    from the local Docker registry (the consistent release source for
    internal services), stops any existing ``lmao-server`` container,
    starts a new one with ``--network host`` and the detected RNode
    device passthrough, and creates a systemd unit at
    ``/etc/systemd/system/lmao-server.service`` so the container starts
    on boot.  The systemd unit references the same registry image.

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

    # ── Pull the release image from the local registry ──
    image = f"{DEFAULT_REGISTRY_HOST}:{DEFAULT_REGISTRY_PORT}/lmao-server:latest"
    print(f"  Pulling {image} ...")
    try:
        pull_proc = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if pull_proc.returncode != 0:
            stderr_tail = pull_proc.stderr.strip().split("\n")[-3:]
            err = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"docker pull {image} failed: {err}")
            print(f"  FAIL: docker pull failed — {err}")
            return
    except subprocess.SubprocessError as exc:
        result.fail(f"docker pull error: {exc}")
        print(f"  FAIL: {exc}")
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
    # Best-effort: stop the systemd unit first so Restart=always does not
    # resurrect the container while we remove it.
    try:
        subprocess.run(
            ["sudo", "systemctl", "stop", "lmao-server"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.SubprocessError:
        pass
    try:
        existing = _docker_psql("name=lmao-server", all=True)
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
        "PYTHONUNBUFFERED=1",
        "-e",
        f"NATS_SERVER={nats_server}",
        "-e",
        f"LMAO_RNODE_PORT={rnode_port}",
    ]
    if rdevice_exists:
        exec_args.extend(["--device", f"{rnode_port}:{rnode_port}"])
    exec_args.append(image)

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

    # ── Start the service ──
    # Preferred path: start via systemd so `systemctl status lmao-server`
    # is authoritative and Restart=always is managed by systemd.
    # Fallback: direct `docker run -d` when systemd is unavailable.
    started = False
    if systemd_ok:
        print("  Starting service: sudo systemctl start lmao-server")
        try:
            proc = subprocess.run(
                ["sudo", "systemctl", "start", "lmao-server"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0:
                started = True
            else:
                stderr_tail = proc.stderr.strip().split("\n")[-3:]
                err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
                print(f"  WARNING: systemctl start failed \u2014 {err_msg}")
                print("  Falling back to direct docker run ...")
        except subprocess.SubprocessError as exc:
            print(f"  WARNING: systemctl start error \u2014 {exc}")
            print("  Falling back to direct docker run ...")

    if not started:
        run_args = list(exec_args)
        # Change --rm to -d --restart unless-stopped for the direct run
        rm_idx = run_args.index("--rm")
        run_args[rm_idx] = "-d"
        restart_idx = run_args.index("--name")
        run_args.insert(restart_idx, "unless-stopped")
        run_args.insert(restart_idx, "--restart")

        print("  Starting container: {}".format(" ".join(run_args)))
        try:
            proc = subprocess.run(run_args, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                started = True
                print(f"  Container started: {proc.stdout.strip()[:12]}")
            else:
                stderr_tail = proc.stderr.strip().split("\n")[-3:]
                err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
                print(f"  WARNING: docker run failed \u2014 {err_msg}")
                if systemd_ok:
                    print("  Systemd service is installed. Fix the issue then:")
                    print("    sudo systemctl start lmao-server")
        except subprocess.SubprocessError as exc:
            print(f"  WARNING: docker run error \u2014 {exc}")
            if systemd_ok:
                print("  Systemd service is installed. Fix the issue then:")
                print("    sudo systemctl start lmao-server")

    # ── Verify container is running ──
    if started:
        print("  Verifying container...")
        try:
            # The container may take a moment to register with the daemon
            # (especially when started via systemd) — retry a few times.
            container_id = ""
            status = ""
            for attempt in range(5):
                proc = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "--filter",
                        "name=lmao-server",
                        "--format",
                        "{{.ID}} {{.Status}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                fields = proc.stdout.strip().split(None, 1)
                container_id = fields[0][:12] if fields else ""
                status = fields[1] if len(fields) > 1 else ""
                if container_id:
                    break
                if attempt < 4:
                    time.sleep(2)
            if container_id:
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
