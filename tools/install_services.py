"""
Install server-side services: Docker build on the Pi and Kubernetes
manifest application to the cluster.

Provides two public functions that are called by install_all.py's main()
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
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.install_all import DeviceResult


def _find_repo_root() -> Optional[str]:
    """Walk up from this module's directory looking for the repo root.

    Identifies the repo root by locating a ``Dockerfile`` or ``.git``
    directory in an ancestor directory.

    Returns:
        Absolute path to the repo root, or ``None`` if not found.
    """
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(current, "Dockerfile")):
            return current
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def install_pi_server(result: DeviceResult, repo_root: Optional[str] = None) -> None:
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
        result.skip(
            "Docker not found on PATH — install with: apt-get install docker.io"
        )
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


def install_k8s_services(result: DeviceResult, repo_root: Optional[str] = None) -> None:
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

    applied = []

    for manifest in manifests:
        manifest_path = os.path.join(repo_root, manifest)
        if not os.path.isfile(manifest_path):
            if applied:
                print(f"  WARNING: {', '.join(applied)} were already applied.")
                print(
                    "  Manual rollback: kubectl delete -f k8s/<manifest>"
                    " for each applied manifest"
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
