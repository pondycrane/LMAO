"""Tests for lma_core import path — import error handling and __all__ exports.

Run with::

    bazel test //tests:test_lma_core --test_output=all
"""

import sys
import logging
from unittest.mock import MagicMock

import pytest


class TestLmaCoreImportError:
    """Tests that lma_core raises helpful ImportError when protobuf stubs missing."""

    def test_import_error_when_proto_missing(self, caplog, monkeypatch):
        """Importing lma_core should raise ImportError when proto.lma_pb2 is missing."""
        # Skip if proto stubs are available (generated via protoc)
        try:
            import proto.lma_pb2  # noqa: F401
            pytest.skip("proto.lma_pb2 is available — cannot test import error path")
        except ImportError:
            pass

        # Remove proto.lma_pb2 from sys.modules if present
        if "proto.lma_pb2" in sys.modules:
            monkeypatch.delitem(sys.modules, "proto.lma_pb2", raising=False)
        if "lma_core" in sys.modules:
            monkeypatch.delitem(sys.modules, "lma_core", raising=False)
        if "proto" in sys.modules:
            monkeypatch.delitem(sys.modules, "proto", raising=False)

        with pytest.raises(ImportError):
            import lma_core  # noqa: F401

        # The module logs a CRITICAL message with build instructions before raising
        critical_messages = [r.message for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_messages) > 0, "Should log a CRITICAL message"
        combined = " ".join(critical_messages)
        assert "Bazel" in combined, f"CRITICAL message should mention Bazel, got: {combined}"
        assert "protoc" in combined, f"CRITICAL message should mention protoc, got: {combined}"

    def test_import_succeeds_when_proto_present(self):
        """When proto.lma_pb2 is available, lma_core imports without error."""
        # Clear any cached/mocked version of lma_core before importing
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]
        # Module may already be cached; test that importing doesn't raise
        try:
            import lma_core
        except ImportError as e:
            pytest.skip(f"lma_core not importable (proto stubs may be missing): {e}")

        assert hasattr(lma_core, "__all__"), "lma_core should define __all__"

    def test_all_exports_are_importable(self):
        """All names in __all__ are present on the module when importable."""
        # Clear any cached/mocked version of lma_core before importing
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]
        try:
            import lma_core
        except ImportError:
            pytest.skip("lma_core not importable — proto stubs may be missing")

        for name in lma_core.__all__:
            assert hasattr(lma_core, name), (
                f"__all__ lists '{name}' but it is not available on the module"
            )

    def test_grpc_request_types_optional_fallback(self, caplog):
        """Missing gRPC request types in proto.lma_pb2 should warn, not crash."""
        # Clear cached modules to force fresh import
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]

        # Create mock proto.lma_pb2 that will raise ImportError on gRPC types
        mock_pb2 = MagicMock()
        mock_pb2.LMAOEnvelope = MagicMock()
        mock_pb2.TextMessage = MagicMock()
        mock_pb2.SensorReport = MagicMock()
        mock_pb2.SensorReading = MagicMock()
        mock_pb2.CommandRequest = MagicMock()
        mock_pb2.CommandAck = MagicMock()
        mock_pb2.AudioMessage = MagicMock()
        mock_pb2.ImageMessage = MagicMock()
        mock_pb2.CallSignal = MagicMock()
        # Simulate gRPC types not being exported — removing them from mock
        # so the try/except in lma_core triggers ImportError and logs WARNING
        del mock_pb2.SendRequest
        del mock_pb2.SendResponse
        del mock_pb2.SubscribeRequest
        del mock_pb2.SubscribeResponse
        del mock_pb2.TunnelRequest
        del mock_pb2.TunnelResponse
        del mock_pb2.GetIdentityRequest
        del mock_pb2.GetIdentityResponse
        sys.modules["proto.lma_pb2"] = mock_pb2

        # proto.lma_pb2_grpc is already not in sys.modules (we cleared it)

        with caplog.at_level(logging.WARNING):
            import importlib
            # Re-import lma_core — the gRPC try/except should fire warnings
            import lma_core as lma_core_mod
            # Since lma_core is cached, reload it to trigger the import path
            importlib.reload(lma_core_mod)

        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_messages) >= 1, (
            f"Should log at least one WARNING for missing gRPC types, got {len(warning_messages)}"
        )

    def test_all_contains_expected_types(self):
        """__all__ should contain the core message types."""
        # Clear any cached/mocked version of lma_core before importing
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]
        try:
            import lma_core
        except ImportError:
            pytest.skip("lma_core not importable — proto stubs may be missing")

        expected = [
            "LMAOEnvelope",
            "TextMessage",
            "SensorReport",
            "SensorReading",
            "CommandRequest",
            "CommandAck",
            "AudioMessage",
            "ImageMessage",
            "CallSignal",
        ]
        for name in expected:
            assert name in lma_core.__all__, (
                f"Expected '{name}' in lma_core.__all__"
            )


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__] + sys.argv[1:]))
