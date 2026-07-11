"""
Minimal protobuf encoder/decoder for LMAO messages on MicroPython.

µReticulum / Cardputer cannot use the full protobuf library (~2 MB).
This hand-coded encoder handles all LMAOEnvelope payload types defined
in proto/lma.proto.

Wire format:
  LMAOEnvelope:  oneof payload → field number + wire type 2 (length-delimited)
    → bytes of sub-message

Supported sub-messages:
  SensorReport    (field 10)
  CommandRequest  (field 11)
  CommandAck      (field 12)
  TextMessage     (field 20)
  AudioMessage    (field 21)
  ImageMessage    (field 22)
  CallSignal      (field 30)
"""

import struct as _struct

# Sentinel for repeated fields in _decode_proto_message field_map
_REPEATED = object()


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
    """Encode a length-delimited field (string, bytes, or nested message)."""
    length = len(data)
    return encode_varint(length) + data


def _encode_float(value):
    """Encode a 32-bit float (wire type 5, little-endian)."""
    return _struct.pack("<f", value)


def _decode_float(data, offset=0):
    """Decode a 32-bit float from 4 bytes at offset."""
    return _struct.unpack("<f", data[offset : offset + 4])[0]


def _decode_proto_message(data, field_map):
    """Generic protobuf wire-format decoder for simple non-nested messages.

    Interprets varint (wire type 0), length-delimited (wire type 2), and
    32-bit float (wire type 5) fields according to *field_map*.  Unknown
    fields and mismatched wire types are silently skipped when the wire type
    is 0, 2, or 5; any other wire type raises ``ValueError``.

    Args:
        data: Bytes to decode.
        field_map: Dict mapping *field_number* to
            *(wire_type, attr_name, transform_fn, default_value)*
            or
            *(wire_type, attr_name, transform_fn, default_value, _REPEATED)*.

            When the optional *_REPEATED* sentinel is present, multiple
            occurrences of the field are accumulated: appended to a list
            if *default_value* is a list, or merged via ``.update()`` if
            *default_value* is a dict (for protobuf map entries).

    Returns:
        dict with keys from ``field_map`` initialised to their defaults.
    """
    result = {}
    for info in field_map.values():
        result[info[1]] = info[3]
    pos = 0
    while pos < len(data):
        tag, tag_len = decode_varint(data, pos)
        pos += tag_len
        field_number = tag >> 3
        wire_type = tag & 0x07
        info = field_map.get(field_number)
        if info is None:
            # Unknown field — skip if wire type 0, 2, or 5, raise otherwise
            if wire_type == 0:
                _, vlen = decode_varint(data, pos)
                pos += vlen
            elif wire_type == 2:
                length, llen = decode_varint(data, pos)
                pos += llen + length
            elif wire_type == 5:
                pos += 4
            else:
                raise ValueError(f"Unsupported wire type: {wire_type}")
            continue
        expected_wire = info[0]
        attr_name = info[1]
        xform = info[2]
        is_repeated = len(info) >= 5 and info[4] is _REPEATED
        if wire_type != expected_wire:
            # Mismatched wire type — skip if 0, 2, or 5, raise otherwise
            if wire_type == 0:
                _, vlen = decode_varint(data, pos)
                pos += vlen
            elif wire_type == 2:
                length, llen = decode_varint(data, pos)
                pos += llen + length
            elif wire_type == 5:
                pos += 4
            else:
                raise ValueError(f"Unsupported wire type: {wire_type}")
            continue
        if wire_type == 0:  # Varint
            value, vlen = decode_varint(data, pos)
            pos += vlen
        elif wire_type == 2:  # Length-delimited
            length, llen = decode_varint(data, pos)
            pos += llen
            value = data[pos : pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit float
            value = _decode_float(data, pos)
            pos += 4
        else:
            raise ValueError(f"Unsupported wire type: {wire_type}")
        if xform is not None:
            value = xform(value)
        if is_repeated:
            if isinstance(result[attr_name], dict):
                result[attr_name].update(value)
            else:
                result[attr_name].append(value)
        else:
            result[attr_name] = value
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  SensorReport (field 10)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_sensor_reading(sensor_id, value, unit, timestamp_ms):
    """Encode a single SensorReading sub-message."""
    result = bytearray()
    result.extend(encode_field(1, 0, encode_varint(sensor_id)))  # uint32
    result.extend(encode_field(2, 5, _encode_float(value)))  # float
    result.extend(encode_field(3, 2, encode_length_delimited(unit.encode("utf-8"))))  # string
    result.extend(encode_field(4, 0, encode_varint(timestamp_ms)))  # uint64
    return bytes(result)


def encode_sensor_report(node_id, seq, battery, readings):
    """Encode a SensorReport protobuf message.

    readings is a list of dicts: [{sensor_id, value, unit, timestamp_ms}, ...]
    """
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(node_id.encode("utf-8"))))  # string
    result.extend(encode_field(2, 0, encode_varint(seq)))  # uint32
    result.extend(encode_field(3, 5, _encode_float(battery)))  # float
    for r in readings:
        inner = encode_sensor_reading(r["sensor_id"], r["value"], r["unit"], r["timestamp_ms"])
        result.extend(encode_field(4, 2, encode_length_delimited(inner)))  # repeated SensorReading
    return bytes(result)


def decode_sensor_reading(data):
    """Decode a single SensorReading from bytes. Returns dict (never None)."""
    return _decode_proto_message(
        data,
        {
            1: (0, "sensor_id", int, 0),
            2: (5, "value", None, 0.0),
            3: (2, "unit", lambda b: b.decode("utf-8", "replace"), ""),
            4: (0, "timestamp_ms", int, 0),
        },
    )


def decode_sensor_report(data):
    """Decode a SensorReport from protobuf bytes.

    Returns dict with keys: node_id, seq, battery, readings (list of dicts).
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "node_id", lambda b: b.decode("utf-8", "replace"), ""),
            2: (0, "seq", int, 0),
            3: (5, "battery", None, 0.0),
            4: (2, "readings", decode_sensor_reading, [], _REPEATED),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  CommandRequest (field 11)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_command_request(cmd_id, target, action, params, issued_ms, expires_ms):
    """Encode a CommandRequest protobuf message.

    params is a dict of string→string.
    """
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(cmd_id.encode("utf-8"))))  # string
    result.extend(encode_field(2, 2, encode_length_delimited(target.encode("utf-8"))))  # string
    result.extend(encode_field(3, 2, encode_length_delimited(action.encode("utf-8"))))  # string
    # map<string, string> params = 4 — encoded as repeated length-delimited entries
    # each entry is a sub-message: key (field 1) + value (field 2)
    for k, v in params.items():
        entry = bytearray()
        entry.extend(encode_field(1, 2, encode_length_delimited(k.encode("utf-8"))))
        entry.extend(encode_field(2, 2, encode_length_delimited(v.encode("utf-8"))))
        result.extend(encode_field(4, 2, encode_length_delimited(bytes(entry))))
    result.extend(encode_field(5, 0, encode_varint(issued_ms)))  # uint64
    result.extend(encode_field(6, 0, encode_varint(expires_ms)))  # uint64
    return bytes(result)


def _decode_map_entry(data):
    """Decode a protobuf map entry (field 1 = key, field 2 = value)."""
    key = ""
    value = ""
    pos = 0
    while pos < len(data):
        tag, tag_len = decode_varint(data, pos)
        pos += tag_len
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            length, llen = decode_varint(data, pos)
            pos += llen
            s = data[pos : pos + length].decode("utf-8", "replace")
            if field_number == 1:
                key = s
            elif field_number == 2:
                value = s
            pos += length
        else:
            raise ValueError(f"Unsupported wire type in map entry: {wire_type}")
    return key, value


def decode_command_request(data):
    """Decode a CommandRequest from protobuf bytes.

    Returns dict with keys: cmd_id, target, action, params (dict), issued_ms, expires_ms.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "cmd_id", lambda b: b.decode("utf-8", "replace"), ""),
            2: (2, "target", lambda b: b.decode("utf-8", "replace"), ""),
            3: (2, "action", lambda b: b.decode("utf-8", "replace"), ""),
            4: (2, "params", lambda b: dict([_decode_map_entry(b)]), {}, _REPEATED),
            5: (0, "issued_ms", int, 0),
            6: (0, "expires_ms", int, 0),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  CommandAck (field 12)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_command_ack(cmd_id, node_id, success, message):
    """Encode a CommandAck protobuf message."""
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(cmd_id.encode("utf-8"))))  # string
    result.extend(encode_field(2, 2, encode_length_delimited(node_id.encode("utf-8"))))  # string
    result.extend(encode_field(3, 0, encode_varint(1 if success else 0)))  # bool (varint)
    result.extend(encode_field(4, 2, encode_length_delimited(message.encode("utf-8"))))  # string
    return bytes(result)


def decode_command_ack(data):
    """Decode a CommandAck from protobuf bytes.

    Returns dict with keys: cmd_id, node_id, success (bool), message.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "cmd_id", lambda b: b.decode("utf-8", "replace"), ""),
            2: (2, "node_id", lambda b: b.decode("utf-8", "replace"), ""),
            3: (0, "success", bool, False),
            4: (2, "message", lambda b: b.decode("utf-8", "replace"), ""),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TextMessage (field 20)
# ═══════════════════════════════════════════════════════════════════════════════


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


def decode_text_message(data):
    """Decode a TextMessage from protobuf bytes.

    Returns dict with keys: node_id, content, timestamp.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "node_id", lambda b: b.decode("utf-8"), ""),
            2: (2, "content", lambda b: b.decode("utf-8"), ""),
            3: (0, "timestamp", int, 0),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  AudioMessage (field 21)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_audio_message(node_id, audio_data, codec, duration_ms, timestamp):
    """Encode an AudioMessage protobuf message.

    audio_data is bytes (not str).
    """
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(node_id.encode("utf-8"))))  # string
    result.extend(encode_field(2, 2, encode_length_delimited(audio_data)))  # bytes
    result.extend(encode_field(3, 2, encode_length_delimited(codec.encode("utf-8"))))  # string
    result.extend(encode_field(4, 0, encode_varint(duration_ms)))  # uint32
    result.extend(encode_field(5, 0, encode_varint(timestamp)))  # uint64
    return bytes(result)


def decode_audio_message(data):
    """Decode an AudioMessage from protobuf bytes.

    Returns dict with keys: node_id, audio_data (bytes), codec, duration_ms, timestamp.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "node_id", lambda b: b.decode("utf-8", "replace"), ""),
            2: (2, "audio_data", None, b""),
            3: (2, "codec", lambda b: b.decode("utf-8", "replace"), ""),
            4: (0, "duration_ms", int, 0),
            5: (0, "timestamp", int, 0),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ImageMessage (field 22)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_image_message(node_id, image_data, fmt, width, height, timestamp):
    """Encode an ImageMessage protobuf message.

    image_data is bytes.
    """
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(node_id.encode("utf-8"))))  # string
    result.extend(encode_field(2, 2, encode_length_delimited(image_data)))  # bytes
    result.extend(encode_field(3, 2, encode_length_delimited(fmt.encode("utf-8"))))  # string
    result.extend(encode_field(4, 0, encode_varint(width)))  # uint32
    result.extend(encode_field(5, 0, encode_varint(height)))  # uint32
    result.extend(encode_field(6, 0, encode_varint(timestamp)))  # uint64
    return bytes(result)


def decode_image_message(data):
    """Decode an ImageMessage from protobuf bytes.

    Returns dict with keys: node_id, image_data (bytes), format, width, height, timestamp.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "node_id", lambda b: b.decode("utf-8", "replace"), ""),
            2: (2, "image_data", None, b""),
            3: (2, "format", lambda b: b.decode("utf-8", "replace"), ""),
            4: (0, "width", int, 0),
            5: (0, "height", int, 0),
            6: (0, "timestamp", int, 0),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  CallSignal (field 30)
# ═══════════════════════════════════════════════════════════════════════════════

# Enum values for CallSignal.Signal
SIGNAL_OFFER = 0
SIGNAL_ANSWER = 1
SIGNAL_ICE = 2
SIGNAL_HANGUP = 3
SIGNAL_KEEPALIVE = 4


def encode_call_signal(signal, sdp_or_ice, media_type):
    """Encode a CallSignal protobuf message.

    signal is an int (0-4).
    """
    result = bytearray()
    result.extend(encode_field(1, 0, encode_varint(signal)))  # enum (varint)
    result.extend(encode_field(2, 2, encode_length_delimited(sdp_or_ice.encode("utf-8"))))  # string
    result.extend(encode_field(3, 2, encode_length_delimited(media_type.encode("utf-8"))))  # string
    return bytes(result)


def decode_call_signal(data):
    """Decode a CallSignal from protobuf bytes.

    Returns dict with keys: signal (int), sdp_or_ice, media_type.
    """
    return _decode_proto_message(
        data,
        {
            1: (0, "signal", int, 0),
            2: (2, "sdp_or_ice", lambda b: b.decode("utf-8", "replace"), ""),
            3: (2, "media_type", lambda b: b.decode("utf-8", "replace"), ""),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Envelope (top-level wrapper)
# ═══════════════════════════════════════════════════════════════════════════════

# Field numbers for oneof dispatch
FIELD_SENSOR = 10
FIELD_COMMAND = 11
FIELD_ACK = 12
FIELD_TEXT = 20
FIELD_AUDIO = 21
FIELD_IMAGE = 22
FIELD_CALL = 30

# Decoder dispatch table: field_number → decoder function
_DECODERS = {
    FIELD_SENSOR: decode_sensor_report,
    FIELD_COMMAND: decode_command_request,
    FIELD_ACK: decode_command_ack,
    FIELD_TEXT: decode_text_message,
    FIELD_AUDIO: decode_audio_message,
    FIELD_IMAGE: decode_image_message,
    FIELD_CALL: decode_call_signal,
}


def encode_envelope_text(textmessage_bytes):
    """Wrap a TextMessage in an LMAOEnvelope (field 20, wire type 2).

    Returns the full LMAOEnvelope bytes ready for LXMF Content.
    """
    return encode_field(FIELD_TEXT, 2, encode_length_delimited(textmessage_bytes))


def encode_sensor_envelope(node_id, seq, battery, readings):
    """Wrap a SensorReport in an LMAOEnvelope (field 10, wire type 2).

    Returns the full LMAOEnvelope bytes ready for LXMF Content.
    """
    sensor_bytes = encode_sensor_report(node_id, seq, battery, readings)
    return encode_field(FIELD_SENSOR, 2, encode_length_delimited(sensor_bytes))


def decode_envelope(data):
    """Decode an LMAOEnvelope, dispatching to the correct sub-message decoder.

    Returns the decoded sub-message dict, or None if no recognized field found.
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
            value = data[pos : pos + length]
            pos += length
            decoder = _DECODERS.get(field_number)
            if decoder is not None:
                return decoder(value)
            # Unknown field — skip
        elif wire_type == 0:  # Varint — skip
            _, vlen = decode_varint(data, pos)
            pos += vlen
        elif wire_type == 5:  # Fixed32 — skip 4 bytes
            pos += 4
        else:
            # Skip unknown wire type
            break

    return None


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
        return result.get("content") if isinstance(result, dict) else None
    # Fallback: treat raw content as plain text
    print("WARNING: parse_poc_message — protobuf decode returned None, trying raw UTF-8 fallback")
    try:
        text = data.decode("utf-8")
        return text
    except UnicodeDecodeError as e:
        print(f"ERROR: parse_poc_message — both protobuf and UTF-8 decode failed: {e}")
        return None
