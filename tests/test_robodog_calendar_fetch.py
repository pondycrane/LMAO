"""Host tests for the RoboDog calendar-fetch render path — no hardware.

Drives ``robodog_client.ui`` (pure renderer) with a fake ST7305 display and a
real ``cardputer_client/proto/lma_encoder`` CalendarBundle: build the wire bytes
the server would send, decode them the way ``robodog_client/main.py`` does
(``decode_envelope`` -> payload == "calendar" -> events list), then render and
assert the resulting 38*400 row-major 1-bit framebuffer (header + rows + pic
box) bit by bit.

Run from the repo root:
    PYTHONPATH=. python3 -m pytest tests/test_robodog_calendar_fetch.py
"""

from cardputer_client.proto import lma_encoder as enc
from robodog_client import ui

ROW_BYTES = ui.ROW_BYTES
HEIGHT = ui.HEIGHT
WIDTH = ui.WIDTH
FRAME_BYTES = ROW_BYTES * HEIGHT  # 38 * 400 == 15200


class FakeST7305:
    """Mimics robodog_client/st7305.ST7305.show(frame) — records the buffer."""

    def __init__(self):
        self.shown = []  # list of buffers passed to show()
        self.last = None

    def show(self, frame):
        self.shown.append(frame)
        self.last = frame


def make_server_reply_envelope(events, seq=7, watermark_ms=1234567, calendar_id="family"):
    """Build the LMAOEnvelope bytes the server would send back (payload field
    24 = CalendarBundle), mirroring lma_encoder.encode_chart_envelope's shape."""
    bundle = enc.encode_calendar_bundle(
        calendar_id=calendar_id, events=events, watermark_ms=watermark_ms
    )
    env = enc.encode_field(enc.FIELD_SEQ, 0, enc.encode_varint(seq))
    env += enc.encode_field(enc.FIELD_CALENDAR, 2, enc.encode_length_delimited(bundle))
    return env


def make_event(uid, title, start_ms, end_ms=0, deleted=False, pic=""):
    return {
        "uid": uid,
        "title": title,
        "notes": "",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "deleted": deleted,
        "pic": pic,
    }


def pixel_is_set(buf, x, y):
    """Return True when pixel (x, y) is ink (bit 7 = leftmost per byte)."""
    return bool(buf[y * ROW_BYTES + (x >> 3)] & (0x80 >> (x & 7)))


# ── Font / glyph placement (deterministic byte-level checks) ───────────────


class TestGlyphAndDrawText:
    def test_glyph_a_exact_bytes(self):
        # 'A' patterns packed bit7-leftmost: .###. -> 0x70, #...# -> 0x88,
        # ##### -> 0xF8; the 8th cell row is blank.
        assert ui.glyph("A") == bytes([0x70, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0x00])

    def test_draw_text_places_exact_glyph_bytes(self):
        buf = bytearray(FRAME_BYTES)
        ui.draw_text(buf, "A", 0, 0)
        for r in range(8):
            # A single glyph at x=0 y=0 occupies byte 0 of rows 0..7, exactly.
            assert buf[r * ROW_BYTES + 0] == ui.glyph("A")[r], "row %d" % r
        assert ui.draw_text(bytearray(FRAME_BYTES), "A", 0, 0) == ui.CHAR_W

    def test_lowercase_maps_to_uppercase(self):
        a = bytearray(FRAME_BYTES)
        ui.draw_text(a, "dentist", 8, 40)
        b = bytearray(FRAME_BYTES)
        ui.draw_text(b, "DENTIST", 8, 40)
        assert a == b


# ── render_into: header + rows byte layout ─────────────────────────────────


class TestRenderInto:
    def test_header_separator_row_is_full_width(self):
        buf = bytearray(FRAME_BYTES)
        ui.render_into(buf, [])
        y = ui.HEADER_H - 1
        # WIDTH=300 -> 37 full bytes + 4 bits in the 38th byte (0xF0).
        expect = b"\xff" * 37 + b"\xf0"
        assert buf[y * ROW_BYTES : (y + 1) * ROW_BYTES] == expect

    def test_default_header_drawn_without_clock(self):
        buf = bytearray(FRAME_BYTES)
        ui.render_into(buf, [], now_ms=None, header_text=None)
        # "FAMILY CALENDAR" text lives in rows 4..11 (glyph cells).
        header_rows = buf[4 * ROW_BYTES : 12 * ROW_BYTES]
        assert any(o for o in header_rows), "header text should be visible"

    def test_header_text_override(self):
        buf = bytearray(FRAME_BYTES)
        rows = ui.render_into(buf, [], header_text="SCHOOL")
        assert rows == []  # no events -> empty rows metadata

    def test_events_sorted_with_rows_metadata(self):
        import time as _t

        ev = [
            make_event("2", "Later", 2_000_000_000_000),
            make_event("1", "Earlier", 1_000_000_000_000),
        ]
        buf = bytearray(FRAME_BYTES)
        rows = ui.render_into(buf, ev, now_ms=0)
        assert [r["title"] for r in rows] == ["Earlier", "Later"]  # sorted asc
        assert rows[0]["y"] == ui.ROW_Y0
        assert rows[1]["y"] == ui.ROW_Y0 + ui.ROW_PITCH
        # HH:MM derives from the event start in the *local* timezone, so
        # recompute it here rather than hardcoding a zone-dependent value.
        lt = _t.localtime(1_000_000_000_000 // 1000)
        assert rows[0]["time_label"] == "%02d:%02d" % (lt[3], lt[4])

    def test_all_day_event_has_empty_time_label(self):
        buf = bytearray(FRAME_BYTES)
        rows = ui.render_into(buf, [make_event("3", "No Time", 0)])
        assert rows == [{"title": "No Time", "time_label": "", "y": ui.ROW_Y0, "pic": False}]

    def test_deleted_and_past_events_skipped(self):
        now = 5_000_000_000_000
        ev = [
            make_event("del", "Deleted", 1, deleted=True),
            make_event("past", "Over", 1, end_ms=now - 1),
            make_event("keep", "Up next", now + 1, end_ms=now + 60_000),
        ]
        buf = bytearray(FRAME_BYTES)
        rows = ui.render_into(buf, ev, now_ms=now)
        assert [r["title"] for r in rows] == ["Up next"]

    def test_max_rows_caps(self):
        buf = bytearray(FRAME_BYTES)
        events = [make_event(str(i), "E%d" % i, i * 1000) for i in range(12)]
        rows = ui.render_into(buf, events)
        assert len(rows) == ui.MAX_ROWS == 6
        rows2 = ui.render_into(buf, events, max_rows=3)
        assert len(rows2) == 3

    def test_pic_placeholder_box_drawn(self):
        buf = bytearray(FRAME_BYTES)
        ui.render_into(buf, [make_event("1", "With pic", 1000, pic="swim")])
        row_y = ui.ROW_Y0
        x_left = WIDTH - 8 - ui.PIC_W  # box left edge
        assert pixel_is_set(buf, x_left, row_y + 4), "pic box left edge"
        assert pixel_is_set(buf, x_left + ui.PIC_W - 1, row_y + 4), "pic box top edge"

    def test_no_pic_leaves_box_region_blank(self):
        buf = bytearray(FRAME_BYTES)
        ui.render_into(buf, [make_event("1", "No pic", 1000)])
        row_y = ui.ROW_Y0
        x_left = WIDTH - 8 - ui.PIC_W
        assert not pixel_is_set(buf, x_left, row_y + 4)

    def test_offline_prefixes_header(self):
        def header_ink(buf):
            # Count set pixels in the header glyph-band rows (4..11).
            return sum(bin(o).count("1") for o in buf[4 * ROW_BYTES : 12 * ROW_BYTES])

        a = bytearray(FRAME_BYTES)
        ui.render_into(a, [], header_text="A", offline=False)
        b = bytearray(FRAME_BYTES)
        ui.render_into(b, [], header_text="A", offline=True)
        # "A" online vs "OFFLINE A" offline: the prefix adds glyph cells, so the
        # offline header band must carry strictly more ink than the online one.
        assert a != b
        assert header_ink(b) > header_ink(a)

    def test_wrong_sized_buffer_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            ui.render_into(bytearray(16), [])

    def test_frame_size_is_exactly_38x400(self):
        assert ROW_BYTES == 38
        assert FRAME_BYTES == 15200


# ── render(): thin wrapper blits via display.show(buf) ─────────────────────


class TestRender:
    def test_render_blits_framebuffer_to_display(self):
        fake = FakeST7305()
        events = [
            make_event("1", "School run", 1_600_000_000_000, end_ms=1_600_003_600_000),
            make_event("2", "Dentist", 1_600_100_000_000, end_ms=1_600_103_600_000),
        ]
        buf = ui.render(fake, events, now_ms=1_500_000_000_000)
        assert fake.last is buf
        assert len(fake.shown) == 1
        assert isinstance(buf, bytearray)
        assert len(buf) == FRAME_BYTES
        # Header separator + at least two event rows of ink.
        ysep = ui.HEADER_H - 1
        assert buf[ysep * ROW_BYTES : (ysep + 1) * ROW_BYTES] == b"\xff" * 37 + b"\xf0"
        first_row = buf[ui.ROW_Y0 * ROW_BYTES : (ui.ROW_Y0 + 12) * ROW_BYTES]
        assert any(first_row)

    def test_render_ignores_missing_display(self):
        buf = ui.render(None, [make_event("1", "T", 1000)])
        assert len(buf) == FRAME_BYTES

    def test_render_survives_broken_display(self):
        class Broken:
            def show(self, frame):
                raise OSError("no panel")

        buf = ui.render(Broken(), [make_event("1", "T", 1000)])
        assert len(buf) == FRAME_BYTES


# ── Full fetch path: encode request -> decode reply -> render ──────────────


class TestCalendarFetchPath:
    def test_request_envelope_roundtrip(self):
        """The exact envelope main.py builds decodes as a calendar request."""
        req = enc.encode_calendar_request(calendar_id="family", since_ms=123456, max_events=0)
        env = enc.encode_request_envelope(9, "calendar", req, request_ack=True)
        decoded = enc.decode_envelope(env)
        assert decoded["seq"] == 9
        assert decoded["request_ack"] is True
        assert decoded["request"]["kind"] == "calendar"
        assert decoded["request"]["calendar"]["calendar_id"] == "family"
        assert decoded["request"]["calendar"]["since_ms"] == 123456

    def test_decode_calendar_bundle_reply(self):
        events = [
            make_event("a", "Swim", 1_000, pic="swim"),
            make_event("b", "Dentist", 2_000),
        ]
        env = make_server_reply_envelope(events, seq=9, watermark_ms=555)
        decoded = enc.decode_envelope(env)
        assert decoded["payload"] == "calendar"
        assert decoded["seq"] == 9
        assert decoded["watermark_ms"] == 555
        assert [e["title"] for e in decoded["events"]] == ["Swim", "Dentist"]

    def test_render_decoded_server_bundle(self):
        """Server reply -> decode (as main.py's handle_reply) -> ui.render."""
        events = [
            make_event("a", "Swim", 1_600_000_000_000, end_ms=1_600_003_600_000, pic="swim"),
            make_event("b", "Dentist", 1_600_100_000_000, end_ms=1_600_103_600_000),
        ]
        env = make_server_reply_envelope(events, seq=11, watermark_ms=777)
        decoded = enc.decode_envelope(env)
        assert decoded["payload"] == "calendar"

        fake = FakeST7305()
        buf = ui.render(fake, decoded["events"], now_ms=1_500_000_000_000)
        rows = ui.render_into(bytearray(FRAME_BYTES), decoded["events"], now_ms=1_500_000_000_000)
        assert len(rows) == 2
        assert rows[0]["title"] == "Swim"
        assert rows[0]["pic"] is True
        assert rows[1]["pic"] is False
        # The pic-bearing row draws its 16x16 box; the other does not.
        assert pixel_is_set(buf, WIDTH - 8 - ui.PIC_W, ui.ROW_Y0 + 4)
        assert not pixel_is_set(buf, WIDTH - 8 - ui.PIC_W, ui.ROW_Y0 + ui.ROW_PITCH + 4)
        # Time labels are rendered ("HH:MM") below each title.
        assert rows[0]["time_label"]


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
