"""Unit tests for robodog_client.voice.voice_bridge TTS downlink pacing.

Regression guard for issue #267: stream_pcm_to must sleep DOWNLINK_PACE (one
pace sleep per inter-frame gap) instead of bursting the whole reply, which
overran the board's 40-frame decode queue and cut the read-out at ~2.4 s. Also
covers the DOWNLINK_PACE env validation (the bridge's only tunable) and the
tts start/stop anchors + full-stream byte count the voice-loop E2E waits on for
its post-readout re-listen assertion (issue #274).

voice_bridge's runtime deps (fastapi, faster_whisper, numpy, opuslib,
websockets) live in the ops venv (~/xiaozhi/.venv), not in @lmao_pip — so they
are stubbed in sys.modules BEFORE importing the module, mirroring
tests/conftest.py's sys.modules pattern.
"""

import asyncio
import json
import logging
import sys
import time
import types
from unittest.mock import MagicMock

import pytest


class _NpView:
    """Result of the fake numpy.frombuffer: a byte round-trip view."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def tobytes(self) -> bytes:
        return self._data


class _FakeNumpy(types.ModuleType):
    """Minimal numpy stand-in (a real module so mypy accepts sys.modules entry).

    stream_pcm_to exercises numpy only as ``np.frombuffer(...).tobytes()``,
    which for even-length s16le bytes is an identity round-trip — the real
    numpy is not in @lmao_pip, so this keeps the test hermetic. ``int16`` (the
    s16le dtype) and ``bool_``/``ndarray`` sentinels are provided for the
    dtype/type probes the bridge and pytest.approx perform.
    """

    def frombuffer(self, buffer: bytes, dtype=None) -> _NpView:
        return _NpView(buffer)

    def isscalar(self, obj) -> bool:
        # pytest.approx probes sys.modules["numpy"].isscalar on plain floats.
        return isinstance(obj, (int, float, bool, complex, str, bytes, type(None)))

    int16 = "int16"
    float64 = "float64"
    bool_ = bool  # real type — pytest.approx isinstance-probes bool_()/bool_
    ndarray = type("ndarray", (), {})  # real type for isinstance probes


def _install_voice_bridge_mocks() -> None:
    """Stub voice_bridge's non-stdlib deps so the module can be imported."""
    for mod in ("fastapi", "faster_whisper", "opuslib", "websockets"):
        sys.modules.setdefault(mod, MagicMock())
    sys.modules.setdefault("numpy", _FakeNumpy("numpy"))


_install_voice_bridge_mocks()

import robodog_client.voice.voice_bridge as vb  # noqa: E402  (after mocks)


class FakeWs:
    """Minimal WebSocket stand-in with an optional mid-stream failure.

    ``sent`` holds opus audio frames (``send_bytes``) and ``text`` holds the
    JSON control frames (``send_text``) separately, so byte offsets and control
    sequencing (tts start/stop) can each be asserted without cross-contamination.
    """

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list = []
        self.text: list = []
        self.fail_after = fail_after

    async def send_bytes(self, data) -> None:
        if self.fail_after and len(self.sent) + 1 >= self.fail_after:
            raise ConnectionError("device closed")
        self.sent.append(data)

    async def send_text(self, data: str) -> None:
        self.text.append(json.loads(data))


def test_module_imports_cleanly():
    """The module loads under the stubbed deps (smoke for the mock layout)."""
    assert vb.FRAME_BYTES == 2880


# ---------------------------------------------------------------------------
# stream_pcm_to pacing contract (issue #267 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_pcm_paces_each_frame(monkeypatch):
    """One DOWNLINK_PACE sleep between frames — never a burst."""
    pcm = b"\x00" * (vb.FRAME_BYTES * 2 + 500)  # 2 full frames + 1 padded
    ws = FakeWs()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep  # capture before patching (avoids recursion)
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep", lambda d: sleeps.append(d) or real_sleep(0))

    ok = await vb.stream_pcm_to(ws, "s1", pcm)

    assert ok is True
    assert len(ws.sent) == 3  # 2 full + 1 zero-padded trailing frame
    # one pace per inter-frame gap incl. the trailing padded frame — never a burst
    assert sleeps == [0.0582, 0.0582, 0.0582]


@pytest.mark.asyncio
async def test_stream_pcm_returns_false_when_device_ends_mid_tts(monkeypatch, caplog):
    """A failing frame halts the stream (False) and sends no further frames.

    The failure path must NOT log ``tts streamed full``: the READOUT gate
    computes reply_secs from that anchor alone, so a partial-byte log on the
    mid-tts path would make a truncated read-out measure frac ~1.0 and pass —
    the exact #272 regression this PR removes."""
    pcm = b"\x00" * (vb.FRAME_BYTES * 4)
    ws = FakeWs(fail_after=3)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep", lambda d: sleeps.append(d) or real_sleep(0))

    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        ok = await vb.stream_pcm_to(ws, "s1", pcm)

    assert ok is False
    assert "device ended mid-tts" in caplog.text
    assert "tts streamed full" not in caplog.text
    assert len(ws.sent) == 2  # stream halts on the failed frame, no sleep after


@pytest.mark.asyncio
async def test_stream_pcm_empty_buffer_returns_true(monkeypatch):
    """An empty (null) TTS buffer sends no frames but is still a success."""
    ws = FakeWs()
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    ok = await vb.stream_pcm_to(ws, "s1", b"")
    assert ok is True
    assert ws.sent == []


@pytest.mark.asyncio
async def test_stream_pcm_logs_full_stream_bytes(monkeypatch, caplog):
    """A clean full stream reports the exact byte count (issue #274 anchor)."""
    pcm = b"\x00" * (vb.FRAME_BYTES * 3)
    ws = FakeWs()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep", lambda d: real_sleep(0))
    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        ok = await vb.stream_pcm_to(ws, "s1", pcm)
    assert ok is True
    assert len(ws.sent) == 3
    assert f"session=s1 tts streamed full {len(pcm)} bytes" in caplog.text


# ---------------------------------------------------------------------------
# tts start/stop anchors (issue #274 post-readout re-listen signal)
# ---------------------------------------------------------------------------


def _frame_sleeps(monkeypatch) -> None:
    """Neutral stub identically used by the stream tests below."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep", lambda d: real_sleep(0))


@pytest.mark.asyncio
async def test_speak_audio_to_brackets_start_stop(monkeypatch, caplog):
    """Auto-TTS audio stream emits a tts start frame, the opus bytes, a tts stop
    frame, and the `tts stop` journal anchor the E2E waits on."""
    ws = FakeWs()
    _frame_sleeps(monkeypatch)
    pcm = b"\x00" * (vb.FRAME_BYTES * 2)
    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        await vb.speak_audio_to(ws, "s1", pcm)
    assert [f["state"] for f in ws.text] == ["start", "stop"]
    assert len(ws.sent) == 2
    assert "session=s1 tts stop" in caplog.text


@pytest.mark.asyncio
async def test_speak_text_to_brackets_start_stop(monkeypatch, caplog):
    """TTS synthesis path (text reply -> Kokoro audio) also emits the stop frame
    and the `tts stop` anchor."""
    ws = FakeWs()
    _frame_sleeps(monkeypatch)
    monkeypatch.setattr(vb, "call_tts", lambda text: b"\x00" * vb.FRAME_BYTES)
    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        await vb.speak_text_to(ws, "s1", "a short reply")
    assert [f["state"] for f in ws.text] == ["start", "sentence_start", "stop"]
    assert len(ws.sent) == 1
    assert "session=s1 tts stop" in caplog.text


def test_frame_constants_match_firmware_decode_queue():
    """The downlink frame is the 60ms/24kHz/16-bit mono contract (#267)."""
    assert vb.FRAME_MS == 60
    assert vb.FRAME_BYTES == 2880  # 1440 samples * 2 bytes, mono
    assert vb.DOWNLINK_PACE == pytest.approx(0.0582)


# ---------------------------------------------------------------------------
# Reply-text caption for voice replies (state screen RESPONDING state)
# ---------------------------------------------------------------------------


async def _make_brain_single(frame):
    """A one-shot in-memory brain queue (bypasses the real WS brain)."""
    q = asyncio.Queue()
    await q.put(frame)
    return q


@pytest.mark.asyncio
async def test_brain_next_voice_returns_caption(monkeypatch):
    """A voice frame carrying the adapter's reply text yields it as the caption
    (the adapter put the gateway's final text inside the existing voice frame)."""
    import base64

    # _brain_next calls decode_audio_to_pcm24k(audio) -> stub it to an identity
    # pass so we only assert the caption plumbing.
    monkeypatch.setattr(vb, "decode_audio_to_pcm24k", lambda audio: b"PCM:" + audio)
    q = await _make_brain_single(
        {
            "type": "voice",
            "session_id": "s1",
            "audio": base64.b64encode(b"wavdata").decode(),
            "text": "  5 user five  ",
        }
    )
    monkeypatch.setattr(vb, "_brain_queue", q)
    kind, payload, caption = await vb._brain_next(time.monotonic() + 10.0, "s1", 5.0)
    assert kind == "voice"
    assert payload == b"PCM:wavdata"
    assert caption == "5 user five"  # whitespace-trimmed via the bridge


@pytest.mark.asyncio
async def test_brain_next_voice_caption_falls_back_to_reply_text(monkeypatch):
    """When the voice frame carries no text, the reply text read during the
    text-vs-voice grace window is reused so RESPONDING still renders text."""
    import base64

    monkeypatch.setattr(vb, "decode_audio_to_pcm24k", lambda audio: b"PCM:" + audio)
    q = asyncio.Queue()
    # voice frame without a text field, but a reply frame arrives first
    await q.put({"type": "reply", "session_id": "s1", "text": "twenty"})
    await q.put({"type": "voice", "session_id": "s1", "audio": base64.b64encode(b"wav").decode()})
    monkeypatch.setattr(vb, "_brain_queue", q)
    kind, payload, caption = await vb._brain_next(time.monotonic() + 10.0, "s1", 5.0)
    assert kind == "voice"
    assert caption == "twenty"


@pytest.mark.asyncio
async def test_speak_audio_to_caption_forwards_sentence_start_no_double_audio(monkeypatch, caplog):
    """With a caption, speak_audio_to sends ONE tts/sentence_start (sanitized
    ASCII) inside the same start/stop basket — and NEVER synthesizes audio from
    the caption (no call_tts, no double speech)."""
    ws = FakeWs()
    _frame_sleeps(monkeypatch)
    pcm = b"\x00" * (vb.FRAME_BYTES * 2)
    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        await vb.speak_audio_to(ws, "s1", pcm, caption="  答案 4\ud83d\ude00 says 静  ")
    # ordering: sentence_start (caption) BEFORE the audio start/stop basket
    states = [f["state"] for f in ws.text]
    assert states == ["sentence_start", "start", "stop"]
    assert ws.text[0]["text"] == "4 says"  # CJK/emoji stripped to space, collapsed
    assert len(ws.sent) == 2
    assert "session=s1 caption -> sentence_start" in caplog.text
    # no double audio: only the 2 opus frames from pcm, no extra TTS synthesis
    assert len(ws.text) == 3  # exactly one sentence_start + start + stop


@pytest.mark.asyncio
async def test_speak_audio_to_without_caption_keeps_old_shape(monkeypatch, caplog):
    """No caption -> exactly the historical start/audio/stop bracket (legacy
    adapters without the text field keep working)."""
    ws = FakeWs()
    _frame_sleeps(monkeypatch)
    pcm = b"\x00" * (vb.FRAME_BYTES * 2)
    with caplog.at_level(logging.INFO, logger="voice_bridge"):
        await vb.speak_audio_to(ws, "s1", pcm)
    assert [f["state"] for f in ws.text] == ["start", "stop"]
    assert len(ws.sent) == 2
    assert "caption -> sentence_start" not in caplog.text


def test_panel_ascii_sanitizer_keeps_printable_ascii_only():
    """The RLCD font is basic-Latin: CJK/emoji become spaces, ASCII stays."""
    assert vb._panel_ascii("count 1..2-3!? ok") == "count 1..2-3!? ok"
    assert vb._panel_ascii("") == ""
    assert vb._panel_ascii("静 4 \ud83d\ude00") == "4"
    assert vb._panel_ascii("alpha\tbeta\n\rgamma") == "alpha beta gamma"
    assert vb._panel_ascii("Spaces   collapse") == "Spaces collapse"


# ---------------------------------------------------------------------------
# DOWNLINK_PACE env validation (bridge's only pacing tunable)
# ---------------------------------------------------------------------------


def test_downlink_pace_default_is_97_percent_of_frame():
    assert vb.DOWNLINK_PACE == pytest.approx(vb.FRAME_MS * 0.97 / 1000.0)


def test_resolve_downlink_pace_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("DOWNLINK_PACE", raising=False)
    assert vb._resolve_downlink_pace() == pytest.approx(0.0582)


def test_resolve_downlink_pace_accepts_valid_override(monkeypatch):
    monkeypatch.setenv("DOWNLINK_PACE", "0.06")
    assert vb._resolve_downlink_pace() == pytest.approx(0.06)


@pytest.mark.parametrize("bad", ["", "not-a-number", "0", "-0.05", "0.5"])
def test_resolve_downlink_pace_rejects_invalid(monkeypatch, bad):
    """Blank/non-numeric values raise at import; <=0 re-enables burst (#267)
    and >2x real-time starves the decode queue — all fail loudly."""
    monkeypatch.setenv("DOWNLINK_PACE", bad)
    with pytest.raises(ValueError):
        vb._resolve_downlink_pace()


if __name__ == "__main__":
    # Bazel py_test executes the file directly; the repo convention (see
    # test_lma_encoder.py) is to invoke pytest explicitly so the tests actually run.
    import pytest as _pytest

    sys.exit(_pytest.main([__file__] + sys.argv[1:]))
