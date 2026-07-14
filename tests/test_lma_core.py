"""Tests for lma_core import path — import error handling and __all__ exports.

Run with::

    bazel test //tests:test_lma_core --test_output=all
"""

import logging
import sys
from unittest.mock import MagicMock

import pytest


class TestLmaCoreImportError:
    """Tests that lma_core raises helpful errors when protobuf stubs missing."""

    def test_import_succeeds_even_when_proto_missing(self):
        """import lma_core should succeed even when proto.lma_messages_pb2 is missing.

        Proto stubs are lazy-loaded only on first attribute access, so
        importing the package itself must not fail.
        """
        # Skip if proto stubs are available (generated via protoc)
        try:
            import proto.lma_messages_pb2  # noqa: F401

            pytest.skip("proto.lma_messages_pb2 is available — cannot test missing-proto path")
        except ImportError:
            pass

        # Remove proto.lma_messages_pb2 from sys.modules if present
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]

        # This should NOT raise — proto is lazy
        import lma_core  # noqa: F401

    def test_proto_access_raises_when_missing(self, caplog):
        """Accessing a proto type from lma_core should raise ImportError when stubs missing."""
        # Skip if proto stubs are available
        try:
            import proto.lma_messages_pb2  # noqa: F401

            pytest.skip("proto.lma_messages_pb2 is available — cannot test missing-proto path")
        except ImportError:
            pass

        # Remove proto.lma_messages_pb2 from sys.modules if present
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]

        import lma_core  # noqa: F401

        # Accessing a proto name should raise ImportError
        with pytest.raises(ImportError):
            lma_core.LMAOEnvelope  # noqa: B018

        # The module logs a CRITICAL message with build instructions before raising
        critical_messages = [r.message for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_messages) > 0, "Should log a CRITICAL message"
        combined = " ".join(critical_messages)
        assert "Bazel" in combined, f"CRITICAL message should mention Bazel, got: {combined}"
        assert "protoc" in combined, f"CRITICAL message should mention protoc, got: {combined}"

    def test_import_succeeds_when_proto_present(self):
        """When proto.lma_messages_pb2 is available, lma_core imports without error."""
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
        """All names in __all__ are present on the module when proto stubs available."""
        # Clear any cached/mocked version of lma_core before importing
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]
        try:
            import lma_core
        except ImportError:
            pytest.skip("lma_core not importable — proto stubs may be missing")

        # hasattr triggers __getattr__ which loads proto stubs lazily.
        # If stubs are available, all names should resolve.
        missing = [name for name in lma_core.__all__ if not hasattr(lma_core, name)]
        assert not missing, f"__all__ lists names that are not available on the module: {missing}"

    def test_grpc_request_types_optional_fallback(self):
        """Missing gRPC request types should warn via grpc_types, not crash lma_core."""
        # Clear cached modules to force fresh import
        for mod in list(sys.modules.keys()):
            if mod.startswith("lma_core") or mod.startswith("proto"):
                del sys.modules[mod]

        # Save original module state for restoration
        _original_messages_pb2 = sys.modules.get("proto.lma_messages_pb2", None)
        _original_grpc_pb2 = sys.modules.get("proto.lma_grpc_pb2", None)
        _original_grpc_pb2_grpc = sys.modules.get("proto.lma_grpc_pb2_grpc", None)

        try:
            # Create mock proto.lma_messages_pb2 with ONLY core message types.
            class _MockMessagesPb2:
                LMAOEnvelope = MagicMock()
                TextMessage = MagicMock()
                SensorReport = MagicMock()
                SensorReading = MagicMock()
                CommandRequest = MagicMock()
                CommandAck = MagicMock()
                AudioMessage = MagicMock()
                ImageMessage = MagicMock()
                CallSignal = MagicMock()

            mock_msgs_pb2 = _MockMessagesPb2()
            sys.modules["proto.lma_messages_pb2"] = mock_msgs_pb2

            # Insert sentinel modules for proto.lma_grpc_pb2 and
            # proto.lma_grpc_pb2_grpc that have no attributes.  This prevents
            # Python's import machinery from re-importing the real stubs from
            # disk (which exist in this environment) and forces the ImportError
            # fallback path in grpc_types.py.
            # NOTE: These sentinel keys must match the import paths used in
            # lma_core/grpc_types.py.  If those imports change, the keys
            # here must be updated too.
            _sentinel = type(sys)("proto.lma_grpc_pb2")
            sys.modules["proto.lma_grpc_pb2"] = _sentinel
            _sentinel_grpc = type(sys)("proto.lma_grpc_pb2_grpc")
            sys.modules["proto.lma_grpc_pb2_grpc"] = _sentinel_grpc

            # Now import lma_core.grpc_types — should log warning
            # about missing gRPC stubs.
            import io

            log_capture = io.StringIO()
            handler = logging.StreamHandler(log_capture)
            handler.setLevel(logging.WARNING)
            logging.getLogger().setLevel(logging.WARNING)
            logging.getLogger().addHandler(handler)

            try:
                import importlib

                importlib.import_module("lma_core.grpc_types")
            except Exception:
                pass
            finally:
                logging.getLogger().removeHandler(handler)

            captured = log_capture.getvalue()
            assert len(captured) > 0, (
                "Should log at least one WARNING for missing gRPC types, got empty output"
            )
            assert "gRPC request/response types not found" in captured, (
                f"Expected warning about gRPC types, got: {captured}"
            )
        finally:
            # Restore original module state to prevent test pollution
            for key, orig in [
                ("proto.lma_messages_pb2", _original_messages_pb2),
                ("proto.lma_grpc_pb2", _original_grpc_pb2),
                ("proto.lma_grpc_pb2_grpc", _original_grpc_pb2_grpc),
            ]:
                if orig is not None:
                    sys.modules[key] = orig
                else:
                    sys.modules.pop(key, None)

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
            assert name in lma_core.__all__, f"Expected '{name}' in lma_core.__all__"


if __name__ == "__main__":
    import pytest as _pytest

    sys.exit(_pytest.main([__file__] + sys.argv[1:]))
