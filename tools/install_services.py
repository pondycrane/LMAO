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

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

# Default NATS server for the LMAO server container (overridable via env).
_DEFAULT_NATS_SERVER = "nats://localhost:4222"

if TYPE_CHECKING:
    from tools.install_all import DeviceResult

# Default local Docker registry address (used when --setup-registry is set).
DEFAULT_REGISTRY_HOST = "192.168.0.36"
DEFAULT_REGISTRY_PORT = 5000


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


def _detect_rnode_port() -> str:
    """Auto-detect the RNode serial port.

    Priority: ``LMAO_RNODE_PORT`` env var, then common ports,
    then fallback to ``/dev/ttyUSB0``.
    """
    env_port = os.environ.get("LMAO_RNODE_PORT")
    if env_port:
        return env_port
    for port in ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyACM1"]:
        if os.path.exists(port):
            return port
    return "/dev/ttyUSB0"


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

    The ``NATS_SERVER`` environment variable (default: ``nats://localhost:4222``)
    is passed through so the server can publish to the in-cluster NATS JetStream.
    The RNode device path follows the same detection as the server config
    (``LMAO_RNODE_PORT`` env var, then auto-detect).

    Requires root privileges (via ``sudo``) for systemd setup.

    The caller must pass a ``DeviceResult`` instance as *result*.  On success
    the result is set to OK; on failure it is set to FAIL.  Missing Docker CLI
    on PATH results in SKIP.

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

    # ── Detect RNode port ────────────────────────────────────────────
    rnode_port = _detect_rnode_port()
    rdevice_exists = os.path.exists(rnode_port)
    if rdevice_exists:
        print(f"  RNode detected at: {rnode_port}")
    else:
        print(f"  RNode port {rnode_port} not found — container will start without LoRa.")

    # ── Stop any existing lmao-server container ─────────────────────
    print("  Stopping existing lmao-server container (if any)...")
    try:
        existing = _docker_psql("name=lmao-server")
    except subprocess.SubprocessError as exc:
        result.fail(f"Failed to query Docker containers: {exc}")
        print(f"  FAIL: docker ps failed — {exc}")
        return
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
            result.fail(f"Failed to stop existing container: {exc}")
            print(f"  FAIL: {exc}")
            return

    # ── Build docker run command ────────────────────────────────────
    nats_server = os.environ.get("NATS_SERVER", _DEFAULT_NATS_SERVER)

    docker_args = [
        "docker", "run", "-d",
        "--name", "lmao-server",
        "--restart", "unless-stopped",
        "--network", "host",
        "-e", f"NATS_SERVER={nats_server}",
    ]

    # Pass through LMAO_RNODE_PORT so the container can detect the port
    docker_args.extend(["-e", f"LMAO_RNODE_PORT={rnode_port}"])

    # Pass through RNode USB serial device to the container
    if rdevice_exists:
        docker_args.extend(["--device", f"{rnode_port}:{rnode_port}"])

    docker_args.append("lmao-server:latest")

    print(f"  Starting container: {' '.join(docker_args)}")

    try:
        proc = subprocess.run(
            docker_args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().split("\n")[-3:]
            err_msg = "; ".join(stderr_tail) if stderr_tail else "unknown error"
            result.fail(f"docker run failed: {err_msg}")
            print(f"  FAIL: docker run failed — {err_msg}")
            return

        container_id = proc.stdout.strip()[:12]
        print(f"  Container started: {container_id}")
    except subprocess.SubprocessError as exc:
        result.fail(f"docker run error: {exc}")
        print(f"  FAIL: {exc}")
        return

    # ── Install systemd service for auto-start on boot ──────────────
    print("  Creating systemd service...")

    # Build the ExecStart line — same as above but without -d
    exec_args = [
        "docker", "run", "--rm",
        "--name", "lmao-server",
        "--network", "host",
        "-e", f"NATS_SERVER={nats_server}",
        "-e", f"LMAO_RNODE_PORT={rnode_port}",
    ]
    if rdevice_exists:
        exec_args.extend(["--device", f"{rnode_port}:{rnode_port}"])
    exec_args.append("lmao-server:latest")

    service_unit = f"""[Unit]
Description=LMAO Server — Reticulum/LXMF LoRa mesh gateway
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker stop lmao-server
ExecStartPre=-/usr/bin/docker rm lmao-server
ExecStart={' '.join(exec_args)}
ExecStop=/usr/bin/docker stop lmao-server
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    try:
        # Write the unit file via a temp file and sudo mv
        fd, tmp_path = tempfile.mkstemp(prefix="lmao-server-", suffix=".service")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(service_unit)

            # Copy to systemd directory with sudo
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
            # Clean up temp file if sudo mv didn't run
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

    except subprocess.SubprocessError as exc:
        stderr_hint = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            stderr_tail = exc.stderr.strip().split("\n")[-3:]
            stderr_hint = ": " + "; ".join(stderr_tail)
        elif isinstance(exc, subprocess.TimeoutExpired) and exc.stderr:
            stderr_hint = ": " + exc.stderr.strip()
        result.fail(f"systemd install failed{stderr_hint}")
        print(f"  FAIL: systemd install{stderr_hint}")
        return
    except PermissionError:
        result.fail("systemd install requires sudo — run with sudo or install manually")
        print("  FAIL: Permission denied — re-run with sudo")
        return
    except Exception as exc:
        import traceback

        traceback.print_exc()
        # Clean up temp file if sudo mv didn't run
        if 'tmp_path' in dir() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        result.fail(f"Unexpected error during systemd setup: {exc}")
        print(f"  FAIL: {exc}")
        return

    # ── Verify container is running ─────────────────────────────────
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
            result.ok(f"Container running: {container_id}")
            print(f"  OK: lmao-server running ({container_id})")
        else:
            result.fail("Container exited after start")
            print("  FAIL: Container exited — check `docker logs lmao-server`")
    except subprocess.SubprocessError:
        result.ok(f"Container started: {container_id} (status check skipped)")
        print(f"  OK: Container started: {container_id}")
