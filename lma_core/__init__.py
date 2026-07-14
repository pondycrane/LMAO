"""LMA Core — shared protobuf wrapper library.

Re-exports generated protobuf stubs for LMAO mesh message types.
gRPC transport types are in ``lma_core.grpc_types`` and are NOT
re-exported here — consumers that need gRPC must import from
``lma_core.grpc_types`` directly.

Proto stubs are loaded **lazily** on first access so that importing
``lma_core`` submodules (e.g. ``lma_core.rnode_flasher``) does not
force a proto dependency.  Only ``from lma_core import LMAOEnvelope``
or similar attribute access triggers the proto import.

Requires generated protobuf stubs for ``lma_messages.proto``:
    bazel build //proto:lma_messages_py_proto
or  python -m grpc_tools.protoc -I proto --python_out=proto proto/lma_messages.proto

Set PYTHONPATH to repo root when running outside Bazel.
"""

import logging

_logger = logging.getLogger(__name__)

_PROTO_NAMES = frozenset(
    {
        "LMAOEnvelope",
        "TextMessage",
        "SensorReport",
        "SensorReading",
        "CommandRequest",
        "CommandAck",
        "AudioMessage",
        "ImageMessage",
        "CallSignal",
    }
)

__all__ = sorted(_PROTO_NAMES)


def __getattr__(name):
    """Lazy-load protobuf stubs on first attribute access.

    This allows ``import lma_core`` (or ``import lma_core.rnode_flasher``)
    without triggering proto imports.  Proto stubs are only loaded when
    someone accesses a specific type, e.g. ``from lma_core import LMAOEnvelope``.
    """
    if name in _PROTO_NAMES:
        try:
            import proto.lma_messages_pb2 as _pb2
        except ImportError as exc:
            _logger.critical(
                "Cannot import generated protobuf stubs from 'proto.lma_messages_pb2'. "
                "Generate them with either:\n"
                "  Bazel:  bazel build //proto:lma_messages_py_proto\n"
                "  Manual: protoc --python_out=proto proto/lma_messages.proto\n"
                "Then set PYTHONPATH to repo root when running outside Bazel.\n"
                f"Original error: {exc}"
            )
            raise
        return getattr(_pb2, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
