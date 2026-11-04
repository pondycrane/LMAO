"""Tests for the ONE RD_SHOT frame parser and the shared E2E rig helpers.

Both used to exist as copies: capture.py's serial reader had its own
BEGIN/hex/END state machine, the ESP-Claw E2E had another (with truncated-frame
handling), and the two gates each carried their own `detect_board_port`,
`detect_sound_card` and audio-command wrappers — which had already drifted (the
empty-`E2E_AUDIO_SG` case emitted a space-prefixed command that the shell read
as a command NAME, failing with 127).
"""

import capture
import e2e_rig


def _frame_lines(buf, w=400, h=300):
    hexdump = buf.hex()
    lines = ["RD_SHOT_BEGIN bufsize=%d w=%d h=%d" % (len(buf), w, h)]
    lines += [hexdump[i:i + 64] for i in range(0, len(hexdump), 64)]
    lines.append("RD_SHOT_END")
    return lines


class TestShotAssembler:
    def test_complete_frame_roundtrip(self):
        buf = bytes(range(64))
        asm = capture.ShotAssembler()
        out = None
        for ln in _frame_lines(buf):
            got = asm.feed(ln)
            if got is not None:
                out = got
        assert out is not None
        data, want, w, h = out
        assert data == buf
        assert (want, w, h) == (64, 400, 300)

    def test_lines_before_and_after_the_frame_are_ignored(self):
        buf = b"\x01\x02\x03"
        asm = capture.ShotAssembler()
        results = [asm.feed("I (123) rd_render: rendered 5 of 16 cached")]
        results += [asm.feed(ln) for ln in _frame_lines(buf)]
        results.append(asm.feed("RD_SHOT_BEGIN bufsize=0 w=0 h=0"))
        frames = [r for r in results if r is not None]
        assert len(frames) == 1
        assert frames[0][0] == buf

    def test_truncated_frame_is_discarded_when_validating(self):
        """A forced `rdshot` dump races the firmware's self-tick dump, so the
        first frame after a trigger is often short.  With validate=True it must
        be skipped, and the NEXT complete frame returned."""
        short = b"\xaa" * 8
        full = b"\xbb" * 16
        asm = capture.ShotAssembler(validate=True)
        lines = _frame_lines(short)
        # announce 16 bytes but only deliver 8 -> truncated
        lines[0] = "RD_SHOT_BEGIN bufsize=16 w=400 h=300"
        out = None
        for ln in lines:
            out = asm.feed(ln) or out
        assert out is None
        for ln in _frame_lines(full):
            out = asm.feed(ln) or out
        assert out is not None
        assert out[0] == full

    def test_truncated_frame_is_accepted_when_not_validating(self):
        asm = capture.ShotAssembler(validate=False)
        out = None
        for ln in _frame_lines(b"\xaa" * 8):
            out = asm.feed(ln) or out
        assert out is not None
        assert out[0] == b"\xaa" * 8

    def test_non_hex_lines_inside_a_frame_are_ignored(self):
        asm = capture.ShotAssembler()
        out = None
        for ln in ["RD_SHOT_BEGIN bufsize=2 w=1 h=1", "I (1) noise", "abcd",
                   "RD_SHOT_END"]:
            out = asm.feed(ln) or out
        assert out is not None
        assert out[0] == bytes.fromhex("abcd")


class TestRigHelpers:
    def test_audio_cmd_without_sg_is_bare(self):
        """Empty E2E_AUDIO_SG must emit the bare command: a leading space makes
        the shell treat `'ffmpeg ...'` as one command NAME (exit 127), which
        silently kills playback/capture."""
        cmd = e2e_rig.audio_cmd("ffmpeg -i x", sg="")
        assert cmd == "ffmpeg -i x"
        assert not cmd.startswith(" ")

    def test_audio_cmd_with_sg_wraps_in_quotes(self):
        cmd = e2e_rig.audio_cmd("ffmpeg -i x", sg="sg audio -c")
        assert cmd == "sg audio -c 'ffmpeg -i x'"

    def test_detect_board_port_prefers_env(self, monkeypatch):
        monkeypatch.setenv("ROBODOG_PORT", "/dev/ttyACM9")
        assert e2e_rig.detect_board_port() == "/dev/ttyACM9"

    def test_detect_board_port_auto_means_autodetect(self, monkeypatch):
        monkeypatch.setenv("ROBODOG_PORT", "AUTO")
        # AUTO must fall through to detection, not return the literal string.
        assert e2e_rig.detect_board_port() != "AUTO"

    def test_wave_duration_of_a_short_wav(self, tmp_path):
        import wave

        p = tmp_path / "a.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 8000)
        assert e2e_rig.wave_duration(p) == 0.5

    def test_wave_duration_unreadable_is_zero(self, tmp_path):
        p = tmp_path / "missing.wav"
        assert e2e_rig.wave_duration(p) == 0.0
