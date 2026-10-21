"""Unit tests for robodog_client.voice.e2e_readout (issue #274 read-out metrics).

Commits synthetic 16 kHz s16le WAVs (stdlib wave/array/struct only) and asserts
the active-play-span -> played_fraction contract: a fully read-out reply gives
played_fraction >= 0.85, a ~2.4 s truncated reply (the decode-queue drop #272
removes) sits clearly below that boundary, inter-word gaps <= 0.6 s merge into
one span, and silent/empty/missing inputs yield 0.0 without raising.
"""

import wave
from pathlib import Path

import robodog_client.voice.e2e_readout as ro


def make_wav(path: Path, segments, rate: int = 16000, amb: int = 400, tone: int = 8000) -> Path:
    """Write a mono s16le WAV from ``segments`` = list of ("silence"|"tone", secs).

    A "tone" is a high-amplitude alternating square (rms ~ tone); "silence" is a
    low-amplitude square (rms ~ amb). 16-bit mono at ``rate`` Hz — the mirror of
    the test-mic capture's 16 kHz."""
    import array

    a = array.array("h")
    for kind, secs in segments:
        n = int(secs * rate)
        amp = tone if kind == "tone" else amb
        a.extend([amp if (i % 2 == 0) else -amp for i in range(n)])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())
    return path


def test_full_readout_passes_boundary(tmp_path):
    """A 12 s reply fully heard yields a ~12 s span -> fraction >= 0.85."""
    f = make_wav(tmp_path / "full.wav", [("silence", 1.0), ("tone", 12.0), ("silence", 8.0)])
    span, start, end = ro.active_play_span(f)
    assert span >= 10.0  # near the 12 s nominal, not truncated
    assert start >= 0.5  # the reply is past the lead silence
    assert ro.played_fraction(span, 12.0) >= 0.85


def test_truncated_readout_fails_boundary(tmp_path):
    """A reply truncated to ~2.4 s (the #272 decode-drop) clearly fails the gate."""
    f = make_wav(tmp_path / "trunc.wav", [("silence", 1.0), ("tone", 2.4), ("silence", 7.0)])
    span, _, _ = ro.active_play_span(f)
    assert span < 3.6
    assert ro.played_fraction(span, 4.0) < 0.85  # the alarm the gate must raise


def test_inter_word_gaps_merge(tmp_path):
    """0.4 s pauses between reply segments merge into one contiguous span."""
    f = make_wav(
        tmp_path / "gap.wav",
        [("silence", 1.0), ("tone", 3.0), ("silence", 0.4), ("tone", 3.0), ("silence", 5.0)],
    )
    span, _, _ = ro.active_play_span(f)
    assert span >= 5.5  # 0.4 s gap (<=0.6) merged into one ~6.4 s span


def test_silent_capture_is_zero(tmp_path):
    """No speech above the ambient floor -> a zero span, no crash."""
    f = make_wav(tmp_path / "silent.wav", [("silence", 5.0)])
    assert ro.active_play_span(f) == (0.0, 0.0, 0.0)
    assert ro.played_fraction(0.0, 4.0) == 0.0


def test_empty_and_missing_are_zero(tmp_path):
    """Empty (0-frame) and missing files yield zero without raising."""
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
    assert ro.active_play_span(empty) == (0.0, 0.0, 0.0)
    assert ro.active_play_span(tmp_path / "nope.wav") == (0.0, 0.0, 0.0)
    assert ro.wave_rms(tmp_path / "nope.wav") == 0.0


def test_window_rms_timeline_and_levels(tmp_path):
    """window_rms returns montonic timestamps around the ambient/tone levels."""
    f = make_wav(tmp_path / "tl.wav", [("silence", 2.0), ("tone", 2.0)])
    ws = ro.window_rms(f)
    times = [t for t, _ in ws]
    rms = [r for _, r in ws]
    assert times == sorted(times) and times[0] == 0.0
    assert max(rms) > 1000.0  # tone windows well above ambient
    assert min(rms) < 500.0  # ambient windows quiet


def test_played_fraction_guards_nonpositive_reply(tmp_path):
    """reply_secs <= 0 gives 0.0 (never a bogus fraction)."""
    assert ro.played_fraction(3.0, 0) == 0.0
    assert ro.played_fraction(3.0, -1) == 0.0


def test_moved_primitives_retain_semantics(tmp_path):
    """The relocated wave_rms/quiet_floor/energy_burst_window keep working."""
    f = make_wav(tmp_path / "burst.wav", [("silence", 1.0), ("tone", 2.0), ("silence", 3.0)])
    assert ro.wave_rms(f) > 0.0
    assert ro.quiet_floor(f) < ro.wave_rms(f)  # floor immune to the burst
    st, en = ro.energy_burst_window(f, 6.0)
    assert st < en and st <= 1.0 < en  # burst window covers the tone region


if __name__ == "__main__":
    import sys

    import pytest as _pytest

    sys.exit(_pytest.main([__file__] + sys.argv[1:]))
