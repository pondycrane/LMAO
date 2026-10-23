"""Unit tests for robodog_client.voice.voice_bridge TTS downlink pacing.

Regression guard for issue #267: stream_pcm_to must sleep DOWNLINK_PACE (one
pace sleep per inter-frame gap) instead of bursting the whole reply, which
overran the board's 40-frame decode queue and cut the read-out at ~2.4 s. Also
covers the DOWNLINK_PACE env validation (the bridge's only tunable).

voice_bridge's runtime deps (fastapi, faster_whisper, numpy, opuslib,
websockets) live in the ops venv (~/xiaozhi/.venv), not in @lmao_pip — so they
are stubbed in sys.modules BEFORE importing the module, mirroring
tests/conftest.py's sys.modules pattern.
"""

import asyncio
import sys
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
    numpy is not in @lmao_pip, so this keeps the test hermetic.
    """

    def frombuffer(self, buffer: bytes, dtype=None) -> _NpView:
        return _NpView(buffer)


def _install_voice_bridge_mocks() -> None:
    """Stub voice_bridge's non-stdlib deps so the module can be imported."""
    for mod in ("fastapi", "faster_whisper", "opuslib", "websockets"):
        sys.modules.setdefault(mod, MagicMock())
    sys.modules.setdefault("numpy", _FakeNumpy("numpy"))


_install_voice_bridge_mocks()

import robodog_client.voice.voice_bridge as vb  # noqa: E402  (after mocks)


class FakeWs:
    """Minimal WebSocket stand-in with an optional mid-stream failure."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list = []
        self.sent_text: list = []
        self.fail_after = fail_after

    async def send_bytes(self, data) -> None:
        if self.fail_after and len(self.sent) + 1 >= self.fail_after:
            raise ConnectionError("device closed")
        self.sent.append(data)

    async def send_text(self, data) -> None:
        self.sent_text.append(data)


async def _text_frames(ws: FakeWs) -> list:
    """Deserialize the text frames a FakeWs collected, in order."""
    import json

    return [json.loads(f) for f in ws.sent_text]


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
    assert sleeps == [0.0582, 0.0582]  # one pace per inter-frame gap, no burst


@pytest.mark.asyncio
async def test_stream_pcm_returns_false_when_device_ends_mid_tts(monkeypatch):
    """A failing frame halts the stream (False) and sends no further frames."""
    pcm = b"\x00" * (vb.FRAME_BYTES * 4)
    ws = FakeWs(fail_after=3)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep", lambda d: sleeps.append(d) or real_sleep(0))

    ok = await vb.stream_pcm_to(ws, "s1", pcm)

    assert ok is False
    assert len(ws.sent) == 2  # stream halts on the failed frame, no sleep after


@pytest.mark.asyncio
async def test_stream_pcm_empty_buffer_returns_true(monkeypatch):
    """An empty (null) TTS buffer sends no frames but is still a success."""
    ws = FakeWs()
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    ok = await vb.stream_pcm_to(ws, "s1", b"")
    assert ok is True
    assert ws.sent == []


def test_frame_constants_match_firmware_decode_queue():
    """The downlink frame is the 60ms/24kHz/16-bit mono contract (#267)."""
    assert vb.FRAME_MS == 60
    assert vb.FRAME_BYTES == 2880  # 1440 samples * 2 bytes, mono
    assert vb.DOWNLINK_PACE == pytest.approx(0.0582)


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


# ---------------------------------------------------------------------------
# caption relay (RLCD response-text streaming, no double-TTS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_captions_sends_sentence_start_only():
    """Caption frames become tts/sentence_start — display-only, no audio."""
    ws = FakeWs()
    await vb.relay_captions(ws, "s1", [{"text": "Hello there."}, {"text": ""}, {"text": "Bye"}])
    frames = await _text_frames(ws)
    assert len(ws.sent) == 0  # never any audio bytes
    assert [f.get("type") for f in frames] == ["tts", "tts"]
    assert [f.get("state") for f in frames] == ["sentence_start", "sentence_start"]
    assert [f.get("text") for f in frames] == ["Hello there.", "Bye"]
    assert all(f.get("session_id") == "s1" for f in frames)


@pytest.mark.asyncio
async def test_speak_audio_to_brackets_captions_then_audio(monkeypatch):
    """Voice replies stream captions BEFORE the audio, bracketed by start/stop.

    Importantly the caption relay must not trigger a second TTS pass — the
    sentence_start arrives without a start/stop pair of its own and no audio is
    synthesized by the bridge for it.
    """
    ws = FakeWs()
    streamed = []

    async def _fake_stream(pcm: bytes) -> bool:
        streamed.append(pcm)
        return True

    monkeypatch.setattr(vb, "stream_pcm_to", _fake_stream)
    await vb.speak_audio_to(ws, "s1", b"\x00\x00", [{"text": "The reply."}])
    frames = await _text_frames(ws)
    assert [f.get("type") for f in frames] == ["tts", "tts", "tts"]
    assert [(f.get("state"), f.get("text")) for f in frames] == [
        ("start", None),
        ("sentence_start", "The reply."),
        ("stop", None),
    ]
    assert streamed == [b"\x00\x00"]  # exactly one audio pass for the reply


def test_display_text_ascii_strips_unsupported_chars():
    """CJK/emoji/smart quotes are dropped — the board font is printable-ASCII
    only and a missing glyph garbles the RLCD line (garbage readback)."""
    assert vb.display_text_ascii({"text": "It's 7:30 in the evening."}) == "It's 7:30 in the evening."
    assert vb.display_text_ascii({"text": "네, 고기요!"}) == ", !"
    assert vb.display_text_ascii({"text": "\u26a1 Interrupting \U0001f4bb now."}) == " Interrupting  now."
    assert "".join(vb.display_text_ascii({"text": t}) for t in []) == ""


@pytest.mark.asyncio
async def test_relay_captions_strips_non_ascii_from_display():
    """A CJK/emoji caption reaches the board sanitized, with no audio frames."""
    ws = FakeWs()
    await vb.relay_captions(ws, "s1", [{"text": "고기요 ⚡"}, {"text": "Ok done"}])
    frames = await _text_frames(ws)
    assert len(ws.sent) == 0
    assert [f.get("text") for f in frames] == [" ", "Ok done"]
    assert all(f.get("state") == "sentence_start" for f in frames)
