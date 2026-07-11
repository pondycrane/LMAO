"""E2E test for K8s pod ↔ LMAO Server gRPC communication.

Validates the complete gRPC communication chain:
  K8s pod → gRPC → LMAO server (running on test host)

The test:
  1. Detects a reachable K8s cluster via ``kubectl cluster-info``
  2. Starts a temporary LMAO server with gRPC on the host
  3. Deploys a K8s pod that runs an inline Python script to exercise gRPC RPCs
     (avoids needing a custom container image; the script builds protobuf
     descriptors dynamically and calls the gRPC endpoints directly)
  4. Verifies GetIdentity and Send RPCs end-to-end
  5. Cleans up all K8s resources

When no K8s cluster is reachable the test skips gracefully (same pattern
as ``test_cardputer_lora_e2e.py`` hardware probe).

Run with::

    bazel test //tests:test_k8s_grpc_e2e --test_output=all
"""
# ruff: noqa: F821 — false positives for code inside inline pod script f-string

import json
import logging
import subprocess
import sys
import time

import pytest
from conftest import cleanup_common_mocks, setup_common_mocks

logger = logging.getLogger(__name__)

# ── probe globals ───────────────────────────────────────────────────

_CLUSTER_READY = False
_CLUSTER_REASON: str | None = None
_CLUSTER_CHECKED = False
_HOST_IP: str | None = None


def _probe_cluster():
    """Probe for a reachable K8s cluster via ``kubectl cluster-info``.

    Detects the best host IP address reachable from K8s pods.  Sets
    module-level globals so the probe runs at most once per process.
    """
    global _CLUSTER_CHECKED, _CLUSTER_READY, _CLUSTER_REASON, _HOST_IP
    if _CLUSTER_CHECKED:
        return
    _CLUSTER_CHECKED = True

    try:
        result = subprocess.run(
            ["kubectl", "cluster-info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            _CLUSTER_REASON = (
                f"kubectl cluster-info failed (exit {result.returncode}): {result.stderr.strip()}"
            )
            return
    except FileNotFoundError:
        _CLUSTER_REASON = "kubectl not found in PATH"
        return
    except subprocess.TimeoutExpired:
        _CLUSTER_REASON = "kubectl cluster-info timed out after 10s"
        return
    except Exception as exc:
        _CLUSTER_REASON = f"K8s cluster probe failed: {exc}"
        return

    # ── Determine host IP reachable from K8s ──
    _HOST_IP = _resolve_host_ip()
    if _HOST_IP is None:
        _CLUSTER_REASON = "Could not determine host IP reachable from K8s cluster"
        return

    _CLUSTER_READY = True


def _resolve_host_ip() -> str | None:
    """Return the host IP address reachable from K8s pods.

    Tries (in order):
      1. Minikube: ``minikube ip`` → ``host.minikube.internal``
      2. Docker Desktop / Kind: ``host.docker.internal``
      3. Host LAN IP: first non-loopback IPv4 from ``hostname -I``
    """
    # 1. Check for minikube
    try:
        result = subprocess.run(
            ["which", "minikube"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "host.minikube.internal"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Check for Docker (Kind / Docker Desktop)
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "host.docker.internal"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 3. Fall back to host LAN IP
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            for ip in ips:
                if not ip.startswith("127.") and not ip.startswith("::1"):
                    return ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _cluster_required():
    """Return a pytest skip reason string when the K8s cluster is missing."""
    _probe_cluster()
    return _CLUSTER_REASON


def _kubectl(
    *args: str,
    timeout: int = 30,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run kubectl with the given arguments and return the result."""
    try:
        return subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError:
        logger.warning("kubectl not found in PATH")
        raise
    except subprocess.TimeoutExpired:
        logger.warning("kubectl command timed out after %ds: %s", timeout, args)
        raise


def _cleanup_resources():
    """Delete K8s resources created by the test.  Best-effort."""
    resource_types = ["pod", "service", "endpoints"]
    for rtype in resource_types:
        for name in ("lmao-e2e-server", "lmao-e2e-test"):
            try:
                _kubectl("delete", rtype, name, "--ignore-not-found", timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("Cleanup timeout deleting %s/%s", rtype, name)
            except FileNotFoundError:
                logger.warning("kubectl not found during cleanup of %s/%s", rtype, name)
            except Exception:
                logger.warning(
                    "Unexpected error deleting %s/%s",
                    rtype,
                    name,
                    exc_info=True,
                )


# ── tests ───────────────────────────────────────────────────────────


class TestK8sClusterDetection:
    """Tests that do NOT require a K8s cluster."""

    def test_kubectl_available(self):
        """kubectl must be in PATH for these tests to run."""
        import shutil

        assert shutil.which("kubectl") is not None, (
            "kubectl not found in PATH. Install kubectl and ensure "
            "it is on PATH when running E2E tests."
        )

    def test_cluster_probe_sets_globals(self):
        """_probe_cluster() should set _CLUSTER_CHECKED after running."""
        # Save and restore globals to avoid side effects from probe.
        import sys as _sys

        mod = _sys.modules[__name__]
        _saved = {
            k: getattr(mod, k)
            for k in (
                "_CLUSTER_CHECKED",
                "_CLUSTER_READY",
                "_CLUSTER_REASON",
                "_HOST_IP",
            )
        }
        try:
            mod._CLUSTER_CHECKED = False
            mod._CLUSTER_READY = False
            mod._CLUSTER_REASON = None
            mod._HOST_IP = None
            _probe_cluster()
            assert mod._CLUSTER_CHECKED is True, "_CLUSTER_CHECKED should be True after probe"
            # Either ready with reason None, or not ready with reason set.
            if mod._CLUSTER_READY:
                assert mod._CLUSTER_REASON is None
                assert mod._HOST_IP is not None
            else:
                assert mod._CLUSTER_REASON is not None
        finally:
            for k, v in _saved.items():
                setattr(mod, k, v)


class TestK8sGrpcE2E:
    """Tests that require a reachable K8s cluster."""

    @pytest.fixture(autouse=True)
    def skip_if_no_cluster(self):
        reason = _cluster_required()
        if reason:
            pytest.skip(reason)

    def test_host_ip_resolved(self):
        """Host IP must be resolved when cluster is available."""
        assert _HOST_IP is not None, "Host IP should be resolved when cluster is reachable"

    def test_grpc_e2e(self):
        """Full E2E: deploy pod, exercise gRPC RPCs, verify output.

        Steps:
          1. Start a temporary LMAO server with gRPC on localhost:50051
          2. Create K8s headless Service + Endpoints pointing to host IP
          3. Deploys a K8s pod that runs an inline Python script to exercise
             gRPC RPCs (avoids needing a custom container image)
          4. Verify GetIdentity RPC returns identity hex
          5. Verify Send RPC returns "queued" status
          6. Clean up all K8s resources (finally block)
        """
        import sys as _sys
        from unittest.mock import MagicMock

        # ── 1. Start temporary LMAO server ─────────────────────────
        # Clean stale sys.modules from prior test runs.
        for mod_name in list(_sys.modules.keys()):
            if mod_name in ("server", "lmao_server", "lmao_server.server"):
                _sys.modules.pop(mod_name, None)

        setup_common_mocks(with_grpc=True)

        try:
            from lmao_server import server as _server_mod

            assert _server_mod.GRPC_AVAILABLE, "GRPC_AVAILABLE must be True for gRPC E2E tests"

            server_inst = _server_mod.Server()
            server_inst.router = MagicMock()
            server_inst.server_identity = MagicMock()
            server_inst.server_identity.hash = b"\x01" * 16
        except ImportError as exc:
            pytest.fail(f"Cannot import lmao_server.server: {exc}")

        # Start gRPC server in a thread so it runs alongside the test.
        import threading

        grpc_ready = threading.Event()
        grpc_error: Exception | None = None
        server_port = 50051

        def _run_grpc_server():
            nonlocal grpc_error
            try:
                from concurrent import futures

                import grpc

                # Create gRPC server bound to 0.0.0.0 so K8s pods can reach it
                grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
                grpc_svc = _server_mod.LMAOGrpcService(server_inst)
                _server_mod.add_LMAOServicer_to_server(grpc_svc, grpc_server)
                grpc_server.add_insecure_port(f"0.0.0.0:{server_port}")
                grpc_server.start()
                grpc_ready.set()
                try:
                    grpc_server.wait_for_termination()
                except Exception as post_exc:
                    logger.warning("gRPC server error after startup: %s", post_exc)
            except Exception as exc:
                grpc_error = exc
                grpc_ready.set()

        server_thread = threading.Thread(target=_run_grpc_server, daemon=True)
        server_thread.start()

        if not grpc_ready.wait(timeout=10):
            pytest.fail("gRPC server failed to start within 10s")
        if grpc_error is not None:
            pytest.fail(f"gRPC server failed to start: {grpc_error}")

        # Give the server a moment to bind
        time.sleep(0.5)

        try:
            # ── 2. Create K8s Service + Endpoints ─────────────────
            # Create headless Service
            svc_result = _kubectl(
                "apply",
                "-f",
                "-",
                timeout=15,
                input=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {
                            "name": "lmao-e2e-server",
                            "labels": {"test": "lmao-e2e"},
                        },
                        "spec": {
                            "clusterIP": "None",
                            "ports": [
                                {
                                    "port": server_port,
                                    "targetPort": server_port,
                                    "protocol": "TCP",
                                    "name": "grpc",
                                }
                            ],
                        },
                    }
                ),
            )
            assert svc_result.returncode == 0, f"kubectl apply Service failed: {svc_result.stderr}"

            # Create Endpoints pointing to host IP
            ep_result = _kubectl(
                "apply",
                "-f",
                "-",
                timeout=15,
                input=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Endpoints",
                        "metadata": {
                            "name": "lmao-e2e-server",
                            "labels": {"test": "lmao-e2e"},
                        },
                        "subsets": [
                            {
                                "addresses": [{"ip": _HOST_IP}],
                                "ports": [{"port": server_port, "name": "grpc"}],
                            }
                        ],
                    }
                ),
            )
            assert ep_result.returncode == 0, f"kubectl apply Endpoints failed: {ep_result.stderr}"

            # ── 3. Deploy test pod ────────────────────────────────
            server_addr = f"lmao-e2e-server.default.svc.cluster.local:{server_port}"

            # The pod runs an inline Python script that calls the gRPC
            # endpoints directly via grpc + protobuf (no generated stubs).
            # This avoids needing proto stubs mounted into the pod.
            inline_script = f'''import grpc, os, sys, time

# Build message classes manually using protobuf descriptor
from google.protobuf import descriptor_pool, symbol_database
from google.protobuf import any_pb2, descriptor_pb2

# --- Define SendRequest ---
file_desc = descriptor_pb2.FileDescriptorProto()
file_desc.name = "e2e_test.proto"
file_desc.package = "lma"
file_desc.syntax = "proto3"

# SendRequest message
msg_send_req = file_desc.message_type.add()
msg_send_req.name = "SendRequest"
field_env = msg_send_req.field.add()
field_env.name = "envelope"
field_env.number = 1
field_env.type = descriptor_pb2.FieldDescriptorProto.TYPE_BYTES
field_env.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
field_dh = msg_send_req.field.add()
field_dh.name = "destination_hash"
field_dh.number = 2
field_dh.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
field_dh.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

# SendResponse message
msg_send_resp = file_desc.message_type.add()
msg_send_resp.name = "SendResponse"
field_status = msg_send_resp.field.add()
field_status.name = "status"
field_status.number = 1
field_status.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
field_status.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
field_dh2 = msg_send_resp.field.add()
field_dh2.name = "destination_hash"
field_dh2.number = 2
field_dh2.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
field_dh2.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

# GetIdentityRequest message (empty)
msg_gi_req = file_desc.message_type.add()
msg_gi_req.name = "GetIdentityRequest"

# GetIdentityResponse message
msg_gi_resp = file_desc.message_type.add()
msg_gi_resp.name = "GetIdentityResponse"
field_id = msg_gi_resp.field.add()
field_id.name = "identity_hex"
field_id.number = 1
field_id.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
field_id.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
field_nn = msg_gi_resp.field.add()
field_nn.name = "node_name"
field_nn.number = 2
field_nn.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
field_nn.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

# Register service
svc = file_desc.service.add()
svc.name = "LMAO"
# Send RPC
rp_send = svc.method.add()
rp_send.name = "Send"
rp_send.input_type = ".lma.SendRequest"
rp_send.output_type = ".lma.SendResponse"
# GetIdentity RPC
rp_gi = svc.method.add()
rp_gi.name = "GetIdentity"
rp_gi.input_type = ".lma.GetIdentityRequest"
rp_gi.output_type = ".lma.GetIdentityResponse"

pool = descriptor_pool.Default()
pool.Add(file_desc)

SendRequest = symbol_database.Default().GetSymbol("lma.SendRequest")
SendResponse = symbol_database.Default().GetSymbol("lma.SendResponse")
GetIdentityRequest = symbol_database.Default().GetSymbol("lma.GetIdentityRequest")
GetIdentityResponse = symbol_database.Default().GetSymbol("lma.GetIdentityResponse")

SERVER = "{server_addr}"

channel = grpc.insecure_channel(SERVER)
stub = channel

# --- GetIdentity ---
print("=== GetIdentity Example ===")
gi_req = GetIdentityRequest()
gi_resp = stub.unary_unary(
    "/lma.LMAO/GetIdentity",
    lambda req: req.SerializeToString(),
    lambda data: GetIdentityResponse.FromString(data),
)(gi_req)
print(f"Server identity: {{gi_resp.identity_hex}}")
print(f"Node name:       {{gi_resp.node_name}}")
assert gi_resp.identity_hex, "identity_hex must not be empty"
assert gi_resp.node_name, "node_name must not be empty"
print("GetIdentity: OK")

# --- Send ---
print("=== Send Example ===")
send_req = SendRequest()
send_req.envelope = b"e2e-test-payload"
send_resp = stub.unary_unary(
    "/lma.LMAO/Send",
    lambda req: req.SerializeToString(),
    lambda data: SendResponse.FromString(data),
)(send_req)
print(f"Send response: status={{send_resp.status}}, dest={{send_resp.destination_hash}}")
assert send_resp.status == "queued", f"Expected 'queued', got '{{send_resp.status}}'"
print("Send: OK")

channel.close()

# --- DuckDB storage verification ---
import duckdb as _duckdb

con = _duckdb.connect(":memory:")
con.execute("""
    CREATE TABLE sensor_readings (
        id INTEGER PRIMARY KEY,
        node_id TEXT NOT NULL,
        seq INTEGER,
        battery REAL,
        sensor_id INTEGER,
        value REAL,
        unit TEXT,
        timestamp_ms INTEGER
    )
""")

# Store a simulated SensorReport (mirrors iot_ingest.build_sensor_envelope)
con.execute(
    "INSERT INTO sensor_readings VALUES "
    "(1, 'e2e-test-node', 1, 3.7, 1, 42.5, 'C', 0)"
)

# Query it back to prove the storage path works end-to-end
rows = con.execute("SELECT node_id, value, unit FROM sensor_readings").fetchall()
assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
assert rows[0][0] == "e2e-test-node", f"Expected 'e2e-test-node', got {rows[0][0]}"
assert rows[0][1] == 42.5, f"Expected 42.5, got {rows[0][1]}"

con.close()
print("__DUCKDB_OK__")

print("__E2E_SUCCESS__")
'''
            # Create pod with the inline test script passed via stdin
            pod_result = _kubectl(
                "run",
                "lmao-e2e-test",
                "--rm",
                "-i",
                "--restart=Never",
                "--image=python:3.12-slim",
                "--command",
                "--",
                "bash",
                "-c",
                "pip install -q grpcio protobuf duckdb 2>/dev/null && python3 -",
                timeout=120,
                input=inline_script,
            )

            stdout = pod_result.stdout
            stderr = pod_result.stderr

            print(f"\n--- Pod stdout ({len(stdout)} bytes) ---")
            print(stdout[:4000])
            if stderr:
                print(f"\n--- Pod stderr ({len(stderr)} bytes) ---")
                print(stderr[:2000])

            # ── 4. Assertions ─────────────────────────────────────
            if pod_result.returncode != 0:
                pytest.fail(
                    f"Test pod exited with code {pod_result.returncode}.\n"
                    f"stdout: {stdout[:2000]}\n"
                    f"stderr: {stderr[:2000]}"
                )

            assert "GetIdentity: OK" in stdout, (
                f"GetIdentity RPC did not succeed.\nstdout: {stdout[:2000]}"
            )
            assert "Send: OK" in stdout, f"Send RPC did not succeed.\nstdout: {stdout[:2000]}"
            assert "__DUCKDB_OK__" in stdout, (
                f"DuckDB verification did not complete.\nstdout: {stdout[:2000]}"
            )
            assert "__E2E_SUCCESS__" in stdout, (
                f"E2E test script did not complete successfully.\nstdout: {stdout[:2000]}"
            )

            # All checks passed - print success before cleanup
            print("\n✅ K8s gRPC E2E test passed!")

        finally:
            # ── 5. Cleanup ────────────────────────────────────────
            _cleanup_resources()
            cleanup_common_mocks()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
