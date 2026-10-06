"""
Minimal protobuf encoder/decoder for LMAO messages on MicroPython.

µReticulum / Cardputer cannot use the full protobuf library (~2 MB).
This hand-coded encoder handles all LMAOEnvelope payload types defined
in proto/lma_messages.proto.

Wire format:
  LMAOEnvelope:  envelope control (seq=1, request_ack=2, request=3) +
    oneof payload → field number + wire type 2 (length-delimited) → bytes

Supported sub-messages:
  Request           (field 3; kind: HistoryRequest=1 / CommandRequest=2)
  SensorReport      (field 10)
  DeliveryAck       (field 12)  — unified ack (delivery confirm + command result)
  TextMessage       (field 20)
  AudioMessage      (field 21)
  ImageMessage      (field 22)
  CallSignal        (field 30)
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

# Unit enum (proto ``Unit``) — mirrors lma_messages.proto; stored as varint on
# the wire (field 3 of SensorReading).  Keep in sync with the schema.
UNIT_UNSPECIFIED = 0
UNIT_PERCENT = 1
UNIT_CELSIUS = 2
UNIT_HECTOPASCAL = 3
UNIT_SECONDS = 4
UNIT_VOLTS = 5
UNIT_DBM = 6
UNIT_BOOL = 7


def encode_sensor_reading(sensor_id, value, unit, timestamp_ms):
    """Encode a single SensorReading sub-message.

    ``unit`` is a proto ``Unit`` enum int (wire type 0, varint) — not a string.
    UNIT_UNSPECIFIED (0) is omitted entirely so unitless readings stay small.
    """
    result = bytearray()
    result.extend(encode_field(1, 0, encode_varint(sensor_id)))  # uint32
    result.extend(encode_field(2, 5, _encode_float(value)))  # float
    if unit:  # non-zero enum -> emit; UNIT_UNSPECIFIED is the default
        result.extend(encode_field(3, 0, encode_varint(int(unit))))  # Unit enum
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
            3: (0, "unit", int, 0),  # Unit enum (varint); default UNSPECIFIED
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


def encode_command_request(target, action, params, issued_ms=0):
    """Encode a CommandRequest protobuf message.

    params is a dict of string→string.
    """
    result = bytearray()
    result.extend(encode_field(1, 2, encode_length_delimited(target.encode("utf-8"))))  # string
    result.extend(encode_field(2, 2, encode_length_delimited(action.encode("utf-8"))))  # string
    # map<string, string> params = 3 — encoded as repeated length-delimited entries
    for k, v in params.items():
        entry = bytearray()
        entry.extend(encode_field(1, 2, encode_length_delimited(k.encode("utf-8"))))
        entry.extend(encode_field(2, 2, encode_length_delimited(v.encode("utf-8"))))
        result.extend(encode_field(3, 2, encode_length_delimited(bytes(entry))))
    if issued_ms:
        result.extend(encode_field(4, 0, encode_varint(issued_ms)))  # uint64
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

    Returns dict with keys: target, action, params (dict), issued_ms.
    """
    return _decode_proto_message(
        data,
        {
            1: (2, "target", lambda b: b.decode("utf-8", "replace"), ""),
            2: (2, "action", lambda b: b.decode("utf-8", "replace"), ""),
            3: (2, "params", lambda b: dict([_decode_map_entry(b)]), {}, _REPEATED),
            4: (0, "issued_ms", int, 0),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  DeliveryAck (field 12) — the unified ack (delivery confirm + command result)
# ═══════════════════════════════════════════════════════════════════════════════


def encode_delivery_ack(seq, server_ms=0, success=True, message="", node_id=""):
    """Encode a DeliveryAck protobuf message."""
    result = bytearray()
    if seq:
        result.extend(encode_field(1, 0, encode_varint(seq)))  # uint32
    if server_ms:
        result.extend(encode_field(2, 0, encode_varint(server_ms)))  # uint64
    if success:
        result.extend(encode_field(3, 0, encode_varint(1)))  # bool (varint)
    if message:
        result.extend(encode_field(4, 2, encode_length_delimited(message.encode("utf-8"))))
    if node_id:
        result.extend(encode_field(5, 2, encode_length_delimited(node_id.encode("utf-8"))))
    return bytes(result)


def decode_delivery_ack(data):
    """Decode a DeliveryAck from protobuf bytes.

    Returns dict with keys: seq, server_ms, success (bool), message, node_id.
    """
    return _decode_proto_message(
        data,
        {
            1: (0, "seq", int, 0),
            2: (0, "server_ms", int, 0),
            3: (0, "success", bool, False),
            4: (2, "message", lambda b: b.decode("utf-8", "replace"), ""),
            5: (2, "node_id", lambda b: b.decode("utf-8", "replace"), ""),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  HistoryRequest (inside Request.kind.history) — chart/query fetch
# ═══════════════════════════════════════════════════════════════════════════════


def encode_history_request(count=0, since_ms=0, series=()):
    """Encode a HistoryRequest protobuf message.

    series is an iterable of Series enum ints; empty means all.
    """
    result = bytearray()
    if count:
        result.extend(encode_field(1, 0, encode_varint(count)))  # uint32
    if since_ms:
        result.extend(encode_field(2, 0, encode_varint(since_ms)))  # uint64
    for s in series:
        result.extend(encode_field(3, 0, encode_varint(s)))  # repeated Series (varint)
    return bytes(result)


def decode_history_request(data):
    """Decode a HistoryRequest from protobuf bytes.

    Returns dict with keys: count, since_ms, series (list of ints).
    """
    return _decode_proto_message(
        data,
        {
            1: (0, "count", int, 0),
            2: (0, "since_ms", int, 0),
            3: (0, "series", int, 0, _REPEATED),
        },
    )


def encode_request_message(kind, inner_bytes):
    """Encode a Request message { oneof kind { history = 1; command = 2 } }."""
    if kind == "history":
        return encode_field(1, 2, encode_length_delimited(inner_bytes))
    if kind == "command":
        return encode_field(2, 2, encode_length_delimited(inner_bytes))
    raise ValueError(f"Unknown request kind: {kind!r}")


def decode_request_message(data):
    """Decode a Request message.

    Returns None, or dict {"kind": "history"|"command", <inner fields>}.
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
            blob = data[pos : pos + length]
            pos += length
            if field_number == 1:
                return {"kind": "history", "history": decode_history_request(blob)}
            if field_number == 2:
                return {"kind": "command", "command": decode_command_request(blob)}
        elif wire_type == 0:
            _, vlen = decode_varint(data, pos)
            pos += vlen
        elif wire_type == 5:
            pos += 4
        else:
            break
    return None


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
#  ChartBundle (field 23)
# ═══════════════════════════════════════════════════════════════════════════════

# Repeated uint32 fields are PACKED on the wire (one length-delimited block of
# concatenated varints); the generic _decode_proto_message does not unpack
# them, so ChartBundle has a dedicated decoder.


def encode_chart_bundle(
    node_id, dry, wet, start_ms, period_ms, soil, temp, hum, watered, fmt=1
):
    """Encode a ChartBundle protobuf message (field numbers per the schema).

    ``temp`` is tenths of C; ``soil``/``hum`` are percents; ``watered`` is a
    bitmask over ``soil`` index (oldest first).  Returns bytes ready to be
    wrapped in LMAOEnvelope.chart.
    """
    result = bytearray()

    def packed(values):
        return encode_length_delimited(b"".join(encode_varint(int(v)) for v in values))

    result.extend(encode_field(1, 0, encode_varint(fmt)))
    result.extend(encode_field(2, 2, encode_length_delimited(node_id.encode("utf-8"))))
    result.extend(encode_field(3, 0, encode_varint(dry)))       # uint32
    result.extend(encode_field(4, 0, encode_varint(wet)))       # uint32
    result.extend(encode_field(5, 0, encode_varint(start_ms)))  # uint64
    result.extend(encode_field(6, 0, encode_varint(period_ms)))  # uint32
    result.extend(encode_field(7, 2, packed(soil)))
    result.extend(encode_field(8, 2, packed(temp)))
    result.extend(encode_field(9, 2, packed(hum)))
    result.extend(encode_field(10, 0, encode_varint(watered)))  # uint64
    return bytes(result)


def _decode_packed_varints(block):
    """Decode a packed-varint payload (length-delimited uint32 block) to ints."""
    out = []
    pos = 0
    while pos < len(block):
        v, n = decode_varint(block, pos)
        out.append(v)
        pos += n
    return out


def decode_chart_bundle(data):
    """Decode a ChartBundle from protobuf bytes.

    Returns dict with keys: format, node_id, dry, wet, start_ms, period_ms,
    soil, temp, hum, watered.
    """
    r = {
        "format": 0, "node_id": "", "dry": 0, "wet": 0,
        "start_ms": 0, "period_ms": 0,
        "soil": [], "temp": [], "hum": [], "watered": 0,
    }
    _MAP = {1: "format", 3: "dry", 4: "wet", 5: "start_ms", 6: "period_ms", 10: "watered"}
    _LIST = {7: "soil", 8: "temp", 9: "hum"}
    pos = 0
    while pos < len(data):
        tag, tlen = decode_varint(data, pos)
        pos += tlen
        f = tag >> 3
        wt = tag & 0x07
        if wt == 0:
            v, n = decode_varint(data, pos)
            pos += n
            if f in _MAP:
                r[_MAP[f]] = v
            elif f in _LIST:
                r[_LIST[f]].append(v)  # tolerates unpacked repeated encoding too
        elif wt == 2:
            length, llen = decode_varint(data, pos)
            pos += llen
            blob = data[pos : pos + length]
            pos += length
            if f == 2:
                r["node_id"] = blob.decode("utf-8")
            elif f in _LIST:
                r[_LIST[f]].extend(_decode_packed_varints(blob))
            # else: unknown field, already consumed
        elif wt == 5:
            pos += 4
        else:
            break
    return r


def encode_chart_envelope(node_id, dry, wet, start_ms, period_ms, soil, temp, hum, watered, fmt=1):
    """Wrap a ChartBundle in an LMAOEnvelope (field 23, wire type 2).

    Returns the full LMAOEnvelope bytes ready for LXMF Content.
    """
    bundle = encode_chart_bundle(
        node_id, dry, wet, start_ms, period_ms, soil, temp, hum, watered, fmt
    )
    return encode_field(FIELD_CHART, 2, encode_length_delimited(bundle))


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

# Field numbers: envelope control (outside the payload oneof) + oneof dispatch
FIELD_SEQ = 1          # uint32 — requester correlation
FIELD_REQUEST_ACK = 2  # bool — ask for a DeliveryAck
FIELD_REQUEST = 3      # Request — intent (history / command)
FIELD_SENSOR = 10
FIELD_ACK = 12         # DeliveryAck
FIELD_TEXT = 20
FIELD_AUDIO = 21
FIELD_IMAGE = 22
FIELD_CHART = 23
FIELD_CALL = 30

# Decoder dispatch table: oneof payload field_number → decoder function
_DECODERS = {
    FIELD_SENSOR: decode_sensor_report,
    FIELD_ACK: decode_delivery_ack,
    FIELD_TEXT: decode_text_message,
    FIELD_AUDIO: decode_audio_message,
    FIELD_IMAGE: decode_image_message,
    FIELD_CHART: decode_chart_bundle,
    FIELD_CALL: decode_call_signal,
}


def encode_envelope_text(textmessage_bytes):
    """Wrap a TextMessage in an LMAOEnvelope (field 20, wire type 2).

    Returns the full LMAOEnvelope bytes ready for LXMF Content.
    """
    return encode_field(FIELD_TEXT, 2, encode_length_delimited(textmessage_bytes))


def encode_sensor_envelope(node_id, seq, battery, readings, request_ack=False):
    """Wrap a SensorReport in an LMAOEnvelope (field 10, wire type 2).

    When request_ack is set, also emits the envelope seq (field 1) + request_ack
    (field 2) so the server replies a DeliveryAck for this report.
    """
    result = bytearray()
    if request_ack:
        if seq:
            result.extend(encode_field(FIELD_SEQ, 0, encode_varint(seq)))
        result.extend(encode_field(FIELD_REQUEST_ACK, 0, encode_varint(1)))
    sensor_bytes = encode_sensor_report(node_id, seq, battery, readings)
    result.extend(encode_field(FIELD_SENSOR, 2, encode_length_delimited(sensor_bytes)))
    return bytes(result)


def encode_request_envelope(seq, kind, inner_bytes, request_ack=False):
    """Wrap a Request (history/command) in an LMAOEnvelope (field 3).

    Emits the envelope seq (field 1) + optional request_ack (field 2), then the
    Request message (field 3). Returns the full LMAOEnvelope bytes.
    """
    result = bytearray()
    if seq:
        result.extend(encode_field(FIELD_SEQ, 0, encode_varint(seq)))
    if request_ack:
        result.extend(encode_field(FIELD_REQUEST_ACK, 0, encode_varint(1)))
    req_bytes = encode_request_message(kind, inner_bytes)
    result.extend(encode_field(FIELD_REQUEST, 2, encode_length_delimited(req_bytes)))
    return bytes(result)


def encode_delivery_ack_envelope(seq, node_id="", success=True, message="", server_ms=0):
    """Wrap a DeliveryAck in an LMAOEnvelope (field 12)."""
    ack_bytes = encode_delivery_ack(seq, server_ms, success, message, node_id)
    return encode_field(FIELD_ACK, 2, encode_length_delimited(ack_bytes))


_FIELD_NAMES = {
    FIELD_SENSOR: "sensor",
    FIELD_ACK: "ack",
    FIELD_TEXT: "text",
    FIELD_AUDIO: "audio",
    FIELD_IMAGE: "image",
    FIELD_CHART: "chart",
    FIELD_CALL: "call",
}


def decode_envelope(data):
    """Decode an LMAOEnvelope.

    Returns a dict carrying the envelope-control keys (``seq``, ``request_ack``)
    plus either a ``request`` dict (when LMAOEnvelope.request is set) or the
    decoded oneof payload fields with a ``payload`` name key. Returns None when
    nothing decodable was found.
    """
    out = {"seq": 0, "request_ack": False, "request": None, "payload": None}
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
            if field_number == FIELD_REQUEST:
                out["request"] = decode_request_message(value)
                continue
            decoder = _DECODERS.get(field_number)
            if decoder is not None:
                out["payload"] = _FIELD_NAMES.get(field_number)
                out.update(decoder(value))
            # else: unknown field — skip
        elif wire_type == 0:  # Varint — seq / request_ack
            v, vlen = decode_varint(data, pos)
            pos += vlen
            if field_number == FIELD_SEQ:
                out["seq"] = v
            elif field_number == FIELD_REQUEST_ACK:
                out["request_ack"] = bool(v)
        elif wire_type == 5:  # Fixed32 — skip 4 bytes
            pos += 4
        else:
            break

    if out["request"] is None and out["payload"] is None:
        return None
    return out


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
    # Return content if protobuf produced a dict; fall through to raw-UTF-8 fallback
    if isinstance(result, dict):
        return result.get("content")
    # Fallback: treat raw content as plain text
    print("WARNING: parse_poc_message — protobuf decode returned None, trying raw UTF-8 fallback")
    try:
        text = data.decode("utf-8")
        return text
    except Exception as e:
        # MicroPython builds may not surface UnicodeDecodeError as a builtin
        # name, so catch broadly here (this is the last-resort fallback).
        print(f"ERROR: parse_poc_message — both protobuf and UTF-8 decode failed: {e}")
        return None
