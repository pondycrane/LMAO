#!/usr/bin/env python3
"""Raw-wire probe for the vLLM-Omni duplex realtime endpoint.

Deliberately minimal (no vllm_omni import): it is the reference the device-side
C client will mirror.  Streams a corpus WAV as paced 100 ms PCM16 16 kHz
frames, commits, and measures time-to-first-audio.
"""
import argparse
import asyncio
import base64
import json
import time
import wave
from pathlib import Path

import websockets

_SEEN = set()

TRAIL_MS = 800

TARGET_RATE = 24000

# The MINIMAL session payload the server actually accepts.  Measured: adding
# `modalities`, `turn_detection`, `input_audio_format` or `extra_body` makes
# the update succeed but then silently drop the commit (no
# `input_audio_buffer.committed`, no response).  `session.type` is mandatory —
# without it the update is rejected with `invalid_event` and the session keeps
# its defaults.
SESS = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        "model": "openbmb/MiniCPM-o-4_5",
        "instructions": (
            "You are RoboDog, a friendly voice assistant on a desk display. "
            "Answer immediately in plain spoken prose, in one short sentence. "
            "Never use markdown, headings, bullet lists, asterisks or emoji."
        ),
    },
}


async def run(url, wav, out_pcm, chunk_ms):
    with wave.open(wav, "rb") as w:
        rate = w.getframerate()
        chans = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"[probe] {wav}: need 16-bit PCM, got {width*8}-bit")
    if chans != 1:
        # Downmix: the session input is mono 16 kHz.
        import array
        a = array.array("h", raw)
        raw = array.array("h", [
            (a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, chans)
        ]).tobytes()
    pcm = raw
    if rate != TARGET_RATE:
        # The corpus WAVs are TTS output at 24 kHz; the board's capture path is
        # 16 kHz, so the probe converts the same way to stay faithful.
        import array
        a = array.array("h", pcm)
        n_out = int(len(a) * TARGET_RATE / rate)
        out = array.array("h", bytes(2 * n_out))
        step = rate / float(TARGET_RATE)
        for i in range(n_out):
            pos = i * step
            j = int(pos)
            frac = pos - j
            out[i] = int(a[j] * (1.0 - frac) + a[min(j + 1, len(a) - 1)] * frac)
        pcm = out.tobytes()
    print(f"[probe] input {len(pcm)} B = {len(pcm)/(TARGET_RATE*2):.2f}s @{TARGET_RATE}")

    t0 = time.monotonic()
    events = {}
    audio_bytes = 0
    transcript = ""
    first_audio = None
    first_text = None
    done = asyncio.Event()
    listen_retries = 0

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps(SESS))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "session.created":
                print(f"[probe] session.created after {(time.monotonic()-t0)*1000:.0f} ms")
                # The server applies session.update asynchronously and RESETS
                # the input buffer when it lands, so audio streamed before
                # `session.updated` is discarded and the commit then produces
                # nothing.  Wait for it.
                while True:
                    m2 = json.loads(await ws.recv())
                    if m2.get("type") == "session.updated":
                        print(f"[probe] session.updated after "
                              f"{(time.monotonic()-t0)*1000:.0f} ms")
                        break
                    if m2.get("type") == "error":
                        print("[probe] handshake error:", m2)
                        return
                break
            if msg.get("type") == "error":
                print("[probe] handshake error:", msg)
                return

        step = TARGET_RATE * 2 * chunk_ms // 1000
        end_ms = 0
        for off in range(0, len(pcm), step):
            piece = pcm[off:off + step]
            end_ms += len(piece) * 1000 / (TARGET_RATE * 2)
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(piece).decode(),
            }))
            await asyncio.sleep(chunk_ms / 1000)
        # Trailing silence in the SAME 24 kHz stream: the model decides
        # turn-completeness from what it hears, so give it the ~800 ms quiet
        # tail the board's own VAD already waits for.
        tail = b"\x00\x00" * (TARGET_RATE * TRAIL_MS // 1000)
        for off in range(0, len(tail), step):
            piece = tail[off:off + step]
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(piece).decode(),
            }))
            await asyncio.sleep(chunk_ms / 1000)

        t_commit = time.monotonic()
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await ws.send(json.dumps({"type": "response.create",
                                  "response": {"modalities": ["audio", "text"]}}))
        print(f"[probe] commit+response.create at {(t_commit-t0)*1000:.0f} ms")

        with open(out_pcm, "wb") as f:
            while not done.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=90)
                except asyncio.TimeoutError:
                    print("[probe] timeout waiting for events")
                    break
                msg = json.loads(raw)
                typ = msg.get("type", "?")
                events[typ] = events.get(typ, 0) + 1
                now = (time.monotonic() - t0) * 1000
                if typ not in _SEEN:
                    _SEEN.add(typ)
                    print(f"[probe] EVENT {typ} @{now:.0f}ms {json.dumps(msg)[:300]}")
                if typ in ("response.output_audio_transcript.delta",
                           "response.output_text.delta", "response.text.delta"):
                    d = msg.get("delta") or ""
                    transcript += d
                    if first_text is None:
                        first_text = now
                        print(f"[probe] first text delta at {now:.0f} ms: {d!r}")
                elif typ == "response.output_audio.delta":
                    b = base64.b64decode(msg.get("delta") or msg.get("audio") or "")
                    audio_bytes += len(b)
                    f.write(b)
                    if first_audio is None:
                        first_audio = now
                        print(f"[probe] FIRST AUDIO at {now:.0f} ms "
                              f"({len(b)} B, sr={msg.get('sample_rate_hz')})")
                elif typ == "response.listen":
                    print(f"[probe] decision listen at {now:.0f} ms "
                          f"(re-asking with an explicit response.create)")
                    if listen_retries < 1:
                        listen_retries += 1
                        await ws.send(json.dumps(
                            {"type": "response.create",
                             "response": {"modalities": ["audio", "text"]}}))
                elif typ == "response.speak":
                    print(f"[probe] decision speak at {now:.0f} ms")
                elif typ == "response.done":
                    print(f"[probe] response.done at {now:.0f} ms")
                    done.set()
                elif typ == "error":
                    print("[probe] error:", msg)

        # Release the server-side session slot: `duplex_session.max_sessions`
        # counts admitted sessions, and a dropped socket keeps its slot until
        # the idle timeout, so the next probe would hit
        # `resource_exhausted: duplex_session_capacity_exhausted`.
        try:
            await ws.send(json.dumps({"type": "session.close"}))
            await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            pass

    print(f"[probe] events {events}")
    print(f"[probe] transcript {transcript!r}")
    print(f"[probe] audio {audio_bytes} B = {audio_bytes/48000:.2f}s @24k")
    if first_audio is not None:
        print(f"[probe] TTFB speak-end->first-audio: "
              f"{first_audio - (t_commit-t0)*1000:.0f} ms")
        print(f"[probe] TTFB connect->first-audio: {first_audio:.0f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://spark-5307:8099/v1/realtime")
    ap.add_argument(
        "--wav",
        default=str(Path(__file__).resolve().parents[1] / "voice" / "corpus" / "count.wav"),
    )
    ap.add_argument("--out", default="/tmp/omni_out.pcm")
    ap.add_argument("--chunk-ms", type=int, default=100)
    a = ap.parse_args()
    asyncio.run(run(a.url, a.wav, a.out, a.chunk_ms))


if __name__ == "__main__":
    main()
