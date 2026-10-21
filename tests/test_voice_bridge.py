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
