"""Shared LMAO message decoding utilities.

Provides a single, tested function for decoding LXMF content bytes
into human-readable strings.  Both the server and the human client
use this function instead of duplicating the decode chain.
"""

import logging

from google.protobuf.message import DecodeError

_logger = logging.getLogger(__name__)


def decode_lmao_message(content_bytes: bytes) -> str:
    """Decode LXMF content bytes into a human-readable string.

    Strategy (in order):
        1. Protobuf decode as LMAOEnvelope → extract text.content
        2. If envelope has no 'text' field → fall through
        3. Raw UTF-8 decode (backward compat with non-protobuf senders)
        4. Byte-count placeholder for binary content

    The LMAOEnvelope import is lazy (inside the function) so that tests
    can mock ``lma_core.LMAOEnvelope`` via sys.modules and have the
    mock take effect at call time rather than module-load time.

    Args:
        content_bytes: Raw bytes from the LXMF message.

    Returns:
        A display string (empty string for empty content).
    """
    # Lazy import so sys.modules mocks take effect at call time
    from lma_core import LMAOEnvelope

    if not content_bytes:
        return ""

    assert LMAOEnvelope is not None  # import re-raises on failure
    envelope = LMAOEnvelope()
    try:
        envelope.ParseFromString(content_bytes)
        if envelope.HasField("text"):
            text = envelope.text.content
            _logger.info("Content (protobuf): %s", text)
            return text
        _logger.warning(
            "Envelope contains non-text payload. "
            "Only text messages are supported in this POC. Falling back."
        )
    except DecodeError:
        _logger.warning("Protobuf parse failed, falling back to raw text")

    # Fallback: raw UTF-8 text (backward compat)
    try:
        text = content_bytes.decode("utf-8")
        _logger.info("Content (raw text): %s", text)
        return text
    except UnicodeDecodeError:
        text = f"<non-text: {len(content_bytes)} bytes>"
        _logger.info("Content: %s", text)
        return text
