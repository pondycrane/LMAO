"""
Minimal protobuf encoder/decoder for LMAO TextMessage on MicroPython.

µReticulum / Cardputer cannot use the full protobuf library (~2 MB).
This hand-coded encoder handles only the TextMessage sub-message
at field number 20 within LMAOEnvelope — enough for the POC.

Wire format:
  LMAOEnvelope:  field 20 (TextMessage), wire type 2 (length-delimited)
    → bytes of TextMessage

  TextMessage:
    field 1: node_id   (string, wire type 2)
    field 2: content   (string, wire type 2)
    field 3: timestamp (uint64, wire type 0)
"""


def encode_varint(value):
    """Encode an unsigned integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def decode_varint(data, offset=0):
    """Decode a protobuf varint. Returns (value, bytes_consumed)."""
    result = 0
    shift = 0
    pos = offset
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            return result, pos - offset
        shift += 7
    raise ValueError("Truncated varint")


def encode_field(field_number, wire_type, payload):
    """Encode a protobuf field tag + payload."""
    tag = (field_number << 3) | wire_type
    return encode_varint(tag) + payload


def encode_length_delimited(data):
    """Encode a length-delimited field (string or nested message)."""
    length = len(data)
    return encode_varint(length) + data


def encode_text_message(node_id, content, timestamp):
    """Encode a TextMessage protobuf message.

    Returns bytes ready to be wrapped in LMAOEnvelope.text field.
    """
    result = bytearray()

    # Field 1: node_id (string, wire type 2)
    result.extend(encode_field(1, 2, encode_length_delimited(node_id.encode("utf-8"))))

    # Field 2: content (string, wire type 2)
    result.extend(encode_field(2, 2, encode_length_delimited(content.encode("utf-8"))))

    # Field 3: timestamp (uint64, wire type 0)
    result.extend(encode_field(3, 0, encode_varint(timestamp)))

    return bytes(result)


def encode_envelope_text(textmessage_bytes):
    """Wrap a TextMessage in an LMAOEnvelope (field 20, wire type 2).

    Returns the full LMAOEnvelope bytes ready for LXMF Content.
    """
    return encode_field(20, 2, encode_length_delimited(textmessage_bytes))


def decode_text_message(data):
    """Decode a TextMessage from protobuf bytes.

    Returns dict with keys: node_id, content, timestamp.
    """
    result = {"node_id": "", "content": "", "timestamp": 0}
    pos = 0
    while pos < len(data):
        tag, tag_len = decode_varint(data, pos)
        pos += tag_len
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # Varint
            value, vlen = decode_varint(data, pos)
            pos += vlen
            if field_number == 3:
                result["timestamp"] = value
        elif wire_type == 2:  # Length-delimited
            length, llen = decode_varint(data, pos)
            pos += llen
            value = data[pos:pos + length]
            pos += length
            if field_number == 1:
                result["node_id"] = value.decode("utf-8")
            elif field_number == 2:
                result["content"] = value.decode("utf-8")
        else:
            raise ValueError(f"Unsupported wire type: {wire_type}")

    return result


def decode_envelope(data):
    """Decode an LMAOEnvelope, returning the TextMessage dict if present.

    Returns None if the envelope does not contain a text message.
    """
    pos = 0
    while pos < len(data):
        tag, tag_len = decode_varint(data, pos)
        pos += tag_len
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 2:
            length, llen = decode_varint(data, pos)
            pos += llen
            value = data[pos:pos + length]
            pos += length
            if field_number == 20:  # TextMessage
                return decode_text_message(value)
            else:
                # Skip unknown field
                pass
        elif wire_type == 0:  # Varint — skip
            _, vlen = decode_varint(data, pos)
            pos += vlen
        else:
            # Skip unknown wire type
            break

    return None


# ---- Verbosity control ----

VERBOSE = False  # Set to True to enable debug prints in parse_poc_message()


def _debug(msg):
    """Print debug message when VERBOSE is enabled."""
    if VERBOSE:
        print(msg)


# ---- Convenience function for the POC ----

def make_poc_message(node_id, text, timestamp=None):
    """Create the full protobuf payload for a POC text message.

    Returns bytes suitable for LXMF Content field.
    """
    import time as _time
    if timestamp is None:
        timestamp = int(_time.time() * 1000)

    text_msg = encode_text_message(node_id, text, timestamp)
    return encode_envelope_text(text_msg)


def parse_poc_message(data):
    """Parse a POC message, returning the text content string or None."""
    try:
        result = decode_envelope(data)
    except Exception:
        result = None
    if result is not None:
        _debug("parse_poc_message — protobuf decode success")
        return result["content"]
    # Fallback: treat raw content as plain text
    print("WARNING: parse_poc_message — protobuf decode returned None, trying raw UTF-8 fallback")
    try:
        text = data.decode("utf-8")
        _debug("parse_poc_message — raw UTF-8 decode success (fallback path)")
        return text
    except UnicodeDecodeError as e:
        print(f"ERROR: parse_poc_message — both protobuf and UTF-8 decode failed: {e}")
        return None
