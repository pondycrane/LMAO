"""Host-side read-out metrics for the hermes voice-loop E2E (issue #274).

The RING assertion only re-ASRs the *loudest 1 s window* of the test-mic
capture, so a reply read-out truncated at ~2.4 s (the 40-frame decode-queue
drop the #272 pacing fix removes) still passes as ``RING PASS-WEAK/audible``.
This module measures the **full active-play span** of the reply on the test mic
and compares it to the bridge's reported TTS duration (``reply_secs``) via
``played_fraction`` — the regression coverage for full read-out.

Pure stdlib (array/statistics/wave) so the Bazel unit tests need no numpy or
faster-whisper, matching the ``//robodog_client:voice_bridge`` py_library
pattern. ``wave_rms`` / ``quiet_floor`` / ``energy_burst_window`` are relocated
from ``hermes_voice_e2e.py`` unchanged (shared with the RING path; do not change
their semantics here).
"""

from __future__ import annotations

import array
import statistics
import wave


def wave_rms(path: str) -> float:
    """Root-mean-square of the whole 16-bit pcm file (0.0 on missing/unreadable)."""
    try:
        with wave.open(str(path)) as w:
            data = w.readframes(w.getnframes())
    except Exception:
        return 0.0
    a = array.array("h", data)
    return (sum(x * x for x in a) / len(a)) ** 0.5 if a else 0.0


def quiet_floor(path: str, win: int = 16000, rate: int = 16000) -> float:
    """Robust ambient floor = median of the 1 s window RMS values (immune to a
    speech burst inflating a whole-file average). Relocated unchanged from the
    RING path; ``active_play_span`` uses a lower-quartile floor instead so a
    *long* reply dominating the capture window still yields an ambient floor."""
    import statistics as _st

    try:
        with wave.open(str(path)) as w:
            a = array.array("h", w.readframes(w.getnframes()))
    except Exception:
        return 0.0
    ws = []
    for i in range(0, max(1, len(a) - win), win // 2):
        ws.append((sum(x * x for x in a[i : i + win]) / win) ** 0.5)
    return _st.median(ws) if ws else 0.0


def energy_burst_window(path: str, total_seconds: float, rate: int = 16000):
    """(start, end) around the loudest 1 s window — the TTS reply burst — so
    re-ASR only sees the reply, not room tone. Relocated unchanged."""
    try:
        with wave.open(str(path)) as w:
            a = array.array("h", w.readframes(w.getnframes()))
    except Exception:
        return (0.0, min(total_seconds, 1.0))
    if not a:
        return (0.0, min(total_seconds, 1.0))
    win = rate
    best, best_i = 0.0, 0
    for i in range(0, len(a) - win, win // 2):
        e = sum(x * x for x in a[i : i + win]) / win
        if e > best:
            best, best_i = e, i
    if best == 0:
        return (0.0, min(total_seconds, 1.0))
    start = best_i / rate
    return (max(0, start - 0.3), min(total_seconds, start + min(total_seconds, 1.5) + 0.3))


def window_rms(
    path: str, win: int = 16000, hop: int = 4000, rate: int = 16000
) -> list[tuple[float, float]]:
    """(start_sec, rms) per sliding window, 0.25 s cadence by default."""
    with wave.open(str(path)) as w:
        a = array.array("h", w.readframes(w.getnframes()))
    out: list[tuple[float, float]] = []
    for i in range(0, max(1, len(a) - win + 1), hop):
        seg = a[i : i + win]
        out.append((i / rate, (sum(x * x for x in seg) / win) ** 0.5))
    return out


def active_play_span(
    path: str,
    rate: int = 16000,
    win: int = 4000,
    hop: int = 2000,
    gap: float = 0.6,
    floor_factor: float = 2.0,
    abs_min: float = 60.0,
):
    """Longest contiguous voiced run above the ambient floor, bridging inter-word
    gaps <= ``gap`` s.

    Returns ``(span_s, start_s, end_s)`` — the played span of the reply on the
    test mic. The floor is the *lower-quartile* of per-window RMS (robust to a
    long reply occupying up to ~75% of the capture, where a median would track
    the reply itself and under-count the span); ``abs_min`` guards a totally
    silent/ambient-less capture. The 0.25 s window keeps boundary bleed small so
    a short truncated reply is measured accurately (a 1 s window would pad a
    ~2.4 s truncation up to the 0.85 boundary — the exact false-pass this gate
    removes). ``(0.0, 0.0, 0.0)`` on no speech (never raises on empty/missing).
    """
    try:
        ws = window_rms(path, win=win, hop=hop, rate=rate)
    except Exception:
        return (0.0, 0.0, 0.0)
    if not ws:
        return (0.0, 0.0, 0.0)
    rms = [r for _, r in ws]
    # Floor = low (10th) percentile of per-window RMS. Real captures always pad
    # the reply with a lead and tail of ambient, so the quiet ~10% of windows is
    # ambient regardless of reply length — a long reply filling most of the
    # 15 s-capped capture window would push a QUARTILE floor up to the reply
    # level and under-count the span (the false-fail this gate must not have).
    floor = statistics.quantiles(rms, n=10)[0] if len(rms) >= 10 else min(rms)
    thresh = max(floor * floor_factor, abs_min)

    runs: list[list[float]] = []
    cur: list[float] | None = None
    for t, r in ws:
        if r >= thresh:
            if cur is None:
                cur = [t, t + win / rate]
            else:
                cur[1] = t + win / rate
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    if not runs:
        return (0.0, 0.0, 0.0)

    merged = [runs[0]]
    for seg in runs[1:]:
        if seg[0] - merged[-1][1] <= gap:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    start, end = max(merged, key=lambda seg: seg[1] - seg[0])
    return (end - start, start, end)


def played_fraction(span: float, reply_secs: float) -> float:
    """Fraction of the bridge-reported TTS duration actually heard on the mic."""
    return 0.0 if reply_secs <= 0 else span / reply_secs
