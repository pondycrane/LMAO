"""LMA Core — shared protobuf wrapper library.

Re-exports generated protobuf stubs and provides convenience
functions for encoding/decoding LMAO messages. Both the server
and host-side tests import from this package.
"""

from proto.lma_pb2 import (
    LMAOEnvelope,
    TextMessage,
    SensorReport,
    SensorReading,
    CommandRequest,
    CommandAck,
    AudioMessage,
    ImageMessage,
    CallSignal,
)

__all__ = [
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
