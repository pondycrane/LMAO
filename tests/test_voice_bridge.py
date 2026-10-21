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
        self.fail_after = fail_after

    async def send_bytes(self, data) -> None:
        if self.fail_after and len(self.sent) + 1 >= self.fail_after:
            raise ConnectionError("device closed")
        self.sent.append(data)


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
    monkeypatch.setattr(vb.asyncio, "sleep",
                        lambda d: sleeps.append(d) or real_sleep(0))

    ok = await vb.stream_pcm_to(ws, "s1", pcm)

    assert ok is True
    assert len(ws.sent) == 3           # 2 full + 1 zero-padded trailing frame
    assert sleeps == [0.0582, 0.0582]  # one pace per inter-frame gap, no burst


@pytest.mark.asyncio
async def test_stream_pcm_returns_false_when_device_ends_mid_tts(monkeypatch):
    """A failing frame halts the stream (False) and sends no further frames."""
    pcm = b"\x00" * (vb.FRAME_BYTES * 4)
    ws = FakeWs(fail_after=3)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vb, "DOWNLINK_PACE", 0.0582)
    monkeypatch.setattr(vb.asyncio, "sleep",
                        lambda d: sleeps.append(d) or real_sleep(0))

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
