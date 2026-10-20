#!/usr/bin/env python3
"""Minimal XiaoZhi WebSocket voice bridge for the RoboDog board.

Implements just enough of the XiaoZhi communication protocol
(https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md) to give the
flashed XiaoZhi voice firmware a fully-local conversation loop:

  board --Opus/WebSocket--> bridge --PCM--> faster-whisper (ASR local)
                                --text--> Hermes session (xiaozhi platform: memory/tools)
                                --text--> Kokoro-82M TTS (spark-5307:8004)
  bridge --Opus frames--> board speaker

The brain is a first-class Hermes platform: the bridge speaks a tiny WebSocket
protocol to the xiaozhi platform plugin running inside the hermes-gateway
(HERMES_BRAIN_URL, default ws://127.0.0.1:8101/xiaozhi/brain). Each utterance is
submitted to the board's Hermes session (identity = board Device-Id/MAC) so the
board gets sessions, multi-turn memory, tools/skills and allow-lists. Hermes
auto-TTS (tts.provider: openai -> our Kokoro) returns the spoken reply as audio
(`voice` frame); when auto-TTS is off/fails the final text returns as a `reply`
frame and the bridge synthesizes it with Kokoro itself. If the brain is
unreachable the bridge falls back to the old stateless gx10 call (call_llm) so
the board keeps working.

Also serves the OTA JSON the board fetches to learn our WebSocket URL, so no
xiaozhi.me dependency. This is glue for the LMAO network only; all upstream
sources (whisper, Kokoro/CosyVoice) are fetched, never vendored.

Env overrides:
  BRIDGE_HOST      (default 0.0.0.0)
  BRIDGE_WS_PORT   (default 8000)  websocket endpoint /xiaozhi/v1/
  BRIDGE_OTA_PORT  (default 8003)  HTTP endpoint   /xiaozhi/ota/ and /healthz
  OTA_WS_URL       (default ws://<BRIDGE_HOST>:8000/xiaozhi/v1/)
  WHISPER_MODEL    (default base)  faster-whisper model id
  WHISPER_LANG     (optional)      force language
  LLM_BASE_URL     (default http://gx10-f156:8000/v1  -- Hermes brain model)
  LLM_API_KEY      (default dummy-local-noauth)
  LLM_MODEL        (default deepseek-ai/DeepSeek-V4-Flash-0731)
  LLM_SYSTEM       (default "You are RoboDog, a helpful voice assistant ...")
  TTS_BASE_URL     (default http://127.0.0.1:50000)  OpenAI TTS base (/v1/audio/speech)
  TTS_SPK_ID       (default 中文女)
"""

import asyncio
import io
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import wave

import numpy as np
import opuslib
import websockets
from fastapi import FastAPI, WebSocket
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("voice_bridge")
# Optional duplicate of the INFO+ stream into a plain file — the E2E
# orchestrator's deterministic assertion source (no journalctl churn, no
# journald dependency). Set BRIDGE_LOG_FILE in the service unit.
_LOG_FILE = os.environ.get("BRIDGE_LOG_FILE", "").strip()
if _LOG_FILE:
    try:
        _fh = logging.FileHandler(_LOG_FILE)
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(_fh)
        log.info("voice bridge log file: %s", _LOG_FILE)
    except OSError as e:
        log.warning("could not open BRIDGE_LOG_FILE %s: %s", _LOG_FILE, e)

HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("BRIDGE_WS_PORT", "8000"))
OTA_PORT = int(os.environ.get("BRIDGE_OTA_PORT", "8003"))
LAN_IP = os.environ.get("LAN_IP", "192.168.50.153")
WS_URL = os.environ.get("OTA_WS_URL", f"ws://{LAN_IP}:{WS_PORT}/xiaozhi/v1/")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_LANG = os.environ.get("WHISPER_LANG", "") or None
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://gx10-f156:8000/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "dummy-local-noauth")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731")
LLM_SYSTEM = os.environ.get(
    "LLM_SYSTEM",
    "You are RoboDog, a friendly voice assistant living on an ESP32 desk display. "
    "Keep answers short and natural for spoken output (1-3 sentences).",
)
TTS_BASE_URL = os.environ.get("TTS_BASE_URL", "http://127.0.0.1:50000").rstrip("/")
TTS_SPK_ID = os.environ.get("TTS_SPK_ID", "中文女")
# Speech backend override envs (the shipped config is in
# lmao-robodog-voice.service: local ASR + Kokoro TTS on spark-5307:8004;
# spark-5307:8002 CosyVoice is disabled there — it emits non-speech because
# its torch/transformers dep versions don't match on the ARM64 GB10 host):
#   ASR_BASE_URL   OpenAI-compatible base for /v1/audio/transcriptions
#                  (e.g. http://spark-eebf:PORT) — unset = local faster-whisper
#   ASR_API_KEY / ASR_MODEL
#   TTS_BASE_URL   base for OpenAI-compatible TTS (/v1/audio/speech),
#                  e.g. http://spark-5307:8004 (Kokoro-82M) or a CosyVoice copy
#   TTS_MODE       "openai" (audio file from /v1/audio/speech) or
#                  "cosyvoice" (stock CosyVoice FastAPI /inference_sft pcm)
#   TTS_MODEL / TTS_VOICE / TTS_RESPONSE_FORMAT
ASR_BASE_URL = os.environ.get("ASR_BASE_URL", "").rstrip("/")
ASR_API_KEY = os.environ.get("ASR_API_KEY", "dummy-local-noauth")
ASR_MODEL = os.environ.get("ASR_MODEL", "base")
TTS_MODE = os.environ.get("TTS_MODE", "openai")  # openai (spark) | cosyvoice (stock)
TTS_MODEL = os.environ.get("TTS_MODEL", "cosyvoice2")
TTS_VOICE = os.environ.get("TTS_VOICE", TTS_SPK_ID)
TTS_RESPONSE_FORMAT = os.environ.get("TTS_RESPONSE_FORMAT", "wav")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "dummy-local-noauth")

# Hermes brain: the XiaoZhi voice board is a first-class Hermes platform. The
# bridge keeps the realtime transport (Opus/WS/VAD) and submits each ASR'd
# utterance to the board's Hermes session over this WebSocket. The bridge falls
# back to the stateless gx10 call (LLM_*) when the brain is unreachable.
HERMES_BRAIN_URL = os.environ.get(
    "HERMES_BRAIN_URL", "ws://127.0.0.1:8101/xiaozhi/brain").strip()
HERMES_BRAIN_TIMEOUT = float(os.environ.get("HERMES_BRAIN_TIMEOUT", "300"))
# Stable board identity (MAC preferred). Order: env DEVICE_ID -> hello.device_id
# -> hello.mac -> a one-time uuid persisted at DEVICE_ID_FILE.
DEVICE_ID = os.environ.get("DEVICE_ID", "").strip()
DEVICE_ID_FILE = os.environ.get("DEVICE_ID_FILE", os.path.expanduser("~/xiaozhi/device_id"))

AUDIO_RATE = 24000  # the board codec runs at 24 kHz
FRAME_MS = 60
FRAME_SAMPLES = AUDIO_RATE * FRAME_MS // 1000  # 1440 samples/channel @24 kHz
FRAME_BYTES = FRAME_SAMPLES * 2 * 1  # s16le mono

app = FastAPI(title="LMAO RoboDog VoIP bridge")

_whisper = None
_dec = None  # shared opus decoder (mono 24k); per-connection buffer instead
_enc = None


def get_whisper():
    global _whisper
    if _whisper is None:
        log.info("loading faster-whisper '%s' (cpu, int8) ...", WHISPER_MODEL)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def get_decoder():
    global _dec
    if _dec is None:
        _dec = opuslib.Decoder(AUDIO_RATE, 1)
    return _dec


def get_encoder():
    global _enc
    if _enc is None:
        _enc = opuslib.Encoder(AUDIO_RATE, 1, opuslib.APPLICATION_VOIP)
    return _enc


def log_response(context, fn, err):
    log.error("%s failed: %s", context, err)


def pcm_to_wav(pcm: bytes, rate: int = AUDIO_RATE) -> bytes:
    """RIFF/WAV wrapper for raw s16le mono PCM (std-lib only)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _urlopen_bytes(req, timeout, attempts=4):
    """Retry transient HTTP/network errors (spark services cold-start)."""
    import urllib.error

    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (404, 429, 500, 502, 503, 504):
                raise
        except (TimeoutError, ConnectionError, OSError) as e:
            last = e
        if i < attempts - 1:
            log.info("retrying after %r (attempt %d/%d)", last, i + 1, attempts)
            time.sleep(0.7 * (i + 1))
    raise last if last else RuntimeError("unknown failure")


def _multipart(fields, filename, filedata, ctype="audio/wav") -> bytes:
    """Minimal multipart/form-data body for OpenAI-style file uploads."""
    boundary = "--lmao-bridge-" + os.urandom(8).hex()
    parts = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
        + filedata
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def remote_asr(pcm: bytes, rate: int = AUDIO_RATE) -> str:
    """OpenAI-compatible remote Whisper (spark-hosted)."""
    base = ASR_BASE_URL.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = base + "/audio/transcriptions"
    body, boundary = _multipart({"model": ASR_MODEL}, "audio.wav", pcm_to_wav(pcm, rate))
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {ASR_API_KEY}",
        },
    )
    data = json.loads(_urlopen_bytes(req, 300))
    return data.get("text", "").strip()


def call_asr(pcm_int16: bytes, rate: int = AUDIO_RATE):
    """Transcribe raw s16le PCM at *rate* Hz (remote whisper or local fallback)."""
    if ASR_BASE_URL:
        text = remote_asr(bytes(pcm_int16), rate)
    else:
        import soxr
        audio = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        audio = soxr.resample(audio, rate, 16000, quality="HQ")
        segments, info = get_whisper().transcribe(
            audio, language=WHISPER_LANG, beam_size=1
        )
        text = " ".join(s.text.strip() for s in segments).strip()
    log.info("ASR text=%r", text)
    return text


def decode_audio_to_pcm24k(audio: bytes) -> bytes:
    """Decode an audio file (wav/mp3/opus) to s16le mono @24 kHz for Opus."""
    import soundfile as sf
    import soxr

    data, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    data = data[:, 0]
    if rate != AUDIO_RATE:
        data = soxr.resample(data, rate, AUDIO_RATE, quality="HQ")
    pcm = np.clip(data * 32767.0, -32768.0, 32767.0).astype(np.int16).tobytes()
    return pcm


def call_tts(text: str) -> bytes:
    """TTS -> s16le PCM @24 kHz.

    mode "openai" = spark OpenAI /v1/audio/speech
    mode "cosyvoice" = stock CosyVoice FastAPI (/inference_sft, raw pcm)
    """
    if not text:
        return b""
    if TTS_MODE == "openai":
        base = TTS_BASE_URL.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        body = json.dumps({
            "model": TTS_MODEL,
            "input": text,
            "voice": TTS_VOICE,
            "response_format": TTS_RESPONSE_FORMAT,
            "speed": 1.0,
        }).encode()
        req = urllib.request.Request(
            base + "/audio/speech", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {TTS_API_KEY}"},
        )
        return decode_audio_to_pcm24k(_urlopen_bytes(req, 1200))
    data = urllib.parse.urlencode({"tts_text": text, "spk_id": TTS_SPK_ID}).encode()
    req = urllib.request.Request(f"{TTS_BASE_URL}/inference_sft", data=data)
    return _urlopen_bytes(req, 1200)


def call_llm(user_text: str) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.4,
        "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Hermes brain client: submit ASR'd utterances to the board's Hermes session
# (xiaozhi platform plugin in the hermes-gateway) and stream the spoken reply
# back. Falls back to call_llm (stateless gx10) when the brain is unreachable.
# ---------------------------------------------------------------------------
_brain_ws = None
_brain_connecting = False
_brain_connected = False
_brain_queue = None  # asyncio.Queue: frames from the single reader
_brain_ever_connected = False  # True once a brain link has come up this run


def _load_device_id() -> str:
    """Stable board identity for Hermes sessions (persisted across restarts)."""
    if DEVICE_ID:
        return DEVICE_ID
    try:
        with open(DEVICE_ID_FILE) as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = "device-" + os.urandom(8).hex()
    try:
        with open(DEVICE_ID_FILE, "w") as fh:
            fh.write(value + "\n")
        os.chmod(DEVICE_ID_FILE, 0o600)
    except OSError as e:
        log.warning("could not persist device id to %s: %s", DEVICE_ID_FILE, e)
    return value


async def _brain_connect_loop():
    """Maintain one brain connection; ONE reader task feeds ``_brain_queue``.

    Two concurrent ``recv()`` on a websockets connection raises
    ConcurrencyError, so every frame is read here and turns consume the queue.
    Reconnects keep the persisted identity, so the Hermes session survives.
    """
    global _brain_ws, _brain_connected, _brain_connecting, _brain_queue, _brain_ever_connected
    while True:
        try:
            async with websockets.connect(HERMES_BRAIN_URL,
                                        ping_interval=20, ping_timeout=60) as ws:
                _brain_ws = ws
                _brain_connected = True
                _brain_ever_connected = True
                _brain_queue = asyncio.Queue()
                log.info("Hermes brain connected: %s (device=%s)",
                         HERMES_BRAIN_URL, _load_device_id())
                await ws.send(json.dumps({
                    "type": "hello",
                    "device_id": _load_device_id(),
                    "session_id": "bridge",
                    "firmware": "xiaozhi-bridge",
                }))
                async for raw in ws:
                    try:
                        await _brain_queue.put(json.loads(raw))
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            log.debug("Hermes brain disconnected (%s); reconnecting...", e)
        finally:
            _brain_connected = False
            _brain_connecting = False
            _brain_ws = None
            _brain_queue = None
        await asyncio.sleep(float(os.environ.get("HERMES_BRAIN_RETRY_SECS", "3")))


async def brain_send_json(payload: dict) -> bool:
    try:
        if _brain_ws is not None:
            await _brain_ws.send(json.dumps(payload))
            return True
    except Exception as e:
        log.debug("brain send failed: %s", e)
    return False


async def _brain_next(deadline: float, session_id: str, timeout: float):
    """Next frame addressed to ``session_id`` (or with no/session-agnostic id).

    Returns (frame, remaining_deadline) or raises TimeoutError. Drafts the
    turn's text-vs-voice race by refusing to return a bare text ``reply`` until
    a short grace window confirms no auto-TTS voice frame follows it.
    """
    import base64
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("Hermes brain reply timed out")
        try:
            frame = await asyncio.wait_for(_brain_queue.get() if _brain_queue is not None
                                           else asyncio.sleep(remain), timeout=min(timeout, remain))
        except asyncio.TimeoutError:
            # Only the grace-window peek may time out silently; the outer deadline
            # already raises.
            raise
        sid = frame.get("session_id") or ""
        if sid not in ("", session_id, None):
            continue  # stale turn (barge-in) — skip
        ftype = frame.get("type")
        if ftype == "voice":
            try:
                audio = base64.b64decode(frame["audio"])
            except Exception as e:
                raise RuntimeError(f"bad voice frame: {e}") from e
            return ("voice", decode_audio_to_pcm24k(audio))
        if ftype == "reply":
            text = str(frame.get("text") or "").strip()
            if not text:
                continue
            # The adapter may emit the final text BEFORE the auto-TTS voice
            # frame; hold the text a beat to prefer voice (no double speech).
            try:
                late, late_pcm = await _brain_next(deadline, session_id, 0.6)
                if late == "voice":
                    return late, late_pcm
            except TimeoutError:
                pass
            return ("reply", text)
        if ftype == "error":
            raise RuntimeError(str(frame.get("message") or "unknown brain error"))
        # typing / welcome / pong / stale frames: keep waiting.


async def hermes_turn(session_id: str, device_id: str, text: str):
    """Send one utterance to the board's Hermes session; wait for its reply.

    Returns ``("voice", pcm_bytes)`` when Hermes auto-TTS produced the spoken
    reply (Kokoro WAV -> 24 kHz PCM), else ``("reply", text)`` when the reply
    came back as text (auto-TTS off/failed) for the bridge to synthesize with
    the same Kokoro voice. Raises on error/timeout so the caller can fall back.
    """
    if not HERMES_BRAIN_URL:
        raise RuntimeError("HERMES_BRAIN_URL is not set")
    # Wait briefly for a live brain. If one has EVER connected this run, give it
    # up to ~3s to come back; if the brain was never up, fall back fast (0.5s) so
    # an outage doesn't stall the board behind an extra sleep.
    budget = 3.0 if _brain_ever_connected else 0.5
    waited = 0.0
    while waited < budget:
        if _brain_connected:
            break
        await asyncio.sleep(0.05)
        waited += 0.05
    if not _brain_connected:
        raise RuntimeError("Hermes brain not connected")

    t0 = time.monotonic()
    if not await brain_send_json({
        "type": "user", "text": text, "device_id": device_id, "session_id": session_id,
    }):
        raise RuntimeError("failed to send utterance to Hermes brain")

    try:
        kind, payload = await _brain_next(
            t0 + HERMES_BRAIN_TIMEOUT, session_id, HERMES_BRAIN_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Hermes brain reply timed out after {HERMES_BRAIN_TIMEOUT:.0f}s") from None
    log.info("session=%s Hermes %s reply: %s in %.2fs",
             session_id, kind,
             len(payload) if kind == "voice" else repr(payload), time.monotonic() - t0)
    return (kind, payload)


async def ws_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception:
        # device closed the session mid-reply (user tapped the button) — normal
        return False


# ---------------------------------------------------------------------------
# Active-session registry + module-level stream helpers (shared by the live
# conversation loop and the E2E_STT injection endpoint).
# ---------------------------------------------------------------------------
ACTIVE_SESSIONS = {}  # session_id -> {"ws": WebSocket, "device_id": str}


async def stream_pcm_to(ws: WebSocket, session_id: str, pcm_buf: bytes) -> bool:
    """Opus-encode 24 kHz PCM to the board; False if the device cut out."""
    buf = np.frombuffer(pcm_buf, dtype=np.int16).tobytes()
    for i in range(0, len(buf), FRAME_BYTES):
        chunk = buf[i:i + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
        try:
            await ws.send_bytes(get_encoder().encode(chunk, FRAME_SAMPLES))
        except Exception:
            log.info("session=%s device ended mid-tts (%d/%d bytes)",
                     session_id, i, len(buf))
            return False
        await asyncio.sleep(0.001)
    return True


async def speak_audio_to(ws: WebSocket, session_id: str, pcm_buf: bytes) -> None:
    """Stream a spoken reply (Hermes auto-TTS audio) with tts start/stop brackets."""
    if not await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "start"}):
        return
    await stream_pcm_to(ws, session_id, pcm_buf)
    await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "stop"})


async def speak_text_to(ws: WebSocket, session_id: str, reply: str) -> None:
    """Turn text into Kokoro audio and stream it to the board session."""
    if not await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "start"}):
        return
    if not await ws_send(ws, {
        "session_id": session_id, "type": "tts", "state": "sentence_start", "text": reply,
    }):
        return
    try:
        audio = await asyncio.to_thread(call_tts, reply)
        await stream_pcm_to(ws, session_id, audio)
    except Exception as e:
        log_response("TTS stream", "call_tts", e)
    await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "stop"})


@app.post("/xiaozhi/inject")
async def inject_utterance(payload: dict):
    """E2E fallback: push a canned utterance into the board's live Hermes
    session (issue #267 E2E_STT_TEXT path) — covers brain->TTS->speaker->
    display, skipping only the real-mic/ASR leg. Mirrors handle_conversation's
    stt -> hermes_turn -> speak sequence so the board sees the same frames.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"error": "text required"}
    sid = str(payload.get("session_id") or "").strip()
    entry = ACTIVE_SESSIONS.get(sid) if sid else None
    if entry is None and ACTIVE_SESSIONS:
        sid, entry = next(reversed(ACTIVE_SESSIONS.items()))  # most recent connect
    if entry is None:
        return {"error": "no live board session", "status": 409}
    ws = entry["ws"]
    if not await ws_send(ws, {"session_id": sid, "type": "stt", "text": text}):
        return {"error": "board session ended", "status": 409}
    try:
        kind, payload = await hermes_turn(sid, entry["device_id"], text)
    except Exception as e:
        log.warning("session=%s inject Hermes brain failed (%s)", sid, e)
        return {"error": "hermes failed", "status": 502}
    if kind == "voice":
        await speak_audio_to(ws, sid, payload)
    else:
        await speak_text_to(ws, sid, payload)
    return {"ok": True, "kind": kind}


async def handle_conversation(ws: WebSocket, session_id: str):
    """Wait for listen frames until stop, then run ASR -> LLM -> TTS and stream.

    Audio rate/channels/frame are negotiated from the device's hello (many
    XiaoZhi builds default to 16 kHz). Decoding against a fixed 24 kHz loses
    all audio silently — exactly the "button clicked but nothing heard"
    failure mode.
    """
    decoder = None
    encoder = None
    session_rate = AUDIO_RATE   # uplink rate from the device hello (16 kHz here)
    ul_frame_samples = FRAME_SAMPLES  # uplink decode frame samples
    frame_ms = FRAME_MS
    # Downlink is ALWAYS 24 kHz: the board codec runs at 24 kHz, so sending
    # 24 kHz Opus avoids the firmware's 16->24 resampler (which is what made
    # the audio robotic when downlink matched the 16 kHz uplink).
    d_frame_samples = FRAME_SAMPLES  # 1440 samples @24 kHz / 60 ms
    d_frame_bytes = FRAME_BYTES      # 2880 bytes
    pcm = bytearray()
    speaking = False
    processing = False
    frames_recv = 0
    decode_warned = False
    had_speech = False
    silence_ms = 0
    device_id = _load_device_id()  # Hermes session identity (stable board id)
    VAD_SILENCE_MS = 800  # trailing silence ends an utterance
    VAD_ENERGY = 300      # int16 frame mean-abs energy that counts as speech

    def configure(params: dict):
        nonlocal decoder, encoder, session_rate, ul_frame_samples, frame_ms
        rate = int(params.get("sample_rate", AUDIO_RATE) or AUDIO_RATE)
        fd = int(params.get("frame_duration", FRAME_MS) or FRAME_MS)
        session_rate = rate
        frame_ms = fd
        ul_frame_samples = rate * fd // 1000
        decoder = opuslib.Decoder(rate, 1)
        encoder = opuslib.Encoder(AUDIO_RATE, 1, opuslib.APPLICATION_VOIP)
        log.info("session=%s audio negotiated: up=%dHz/%dms down=%dHz",
                 session_id, rate, fd, AUDIO_RATE)

    async def _stream_pcm(pcm_buf: bytes) -> bool:
        return await stream_pcm_to(ws, session_id, pcm_buf)

    async def _speak_audio(pcm_buf: bytes) -> None:
        await speak_audio_to(ws, session_id, pcm_buf)

    async def _speak_text(reply: str) -> None:
        await speak_text_to(ws, session_id, reply)

    async def finalize():
        """Transcribe buffered audio and stream a reply (realtime full-duplex).

        Brain: the utterance goes to the board's Hermes session (xiaozhi platform,
        device-id keyed) so sessions/memory/tools apply. Hermes auto-TTS returns the
        spoken reply as audio ("voice"), or the reply as text ("reply") which the
        bridge speaks with the same Kokoro voice. On any brain failure we fall back
        to the stateless gx10 call so the board never goes mute.
        """
        nonlocal pcm, processing, had_speech, silence_ms
        processing = True
        try:
            if not pcm:
                return
            buf = np.frombuffer(bytes(pcm), dtype=np.int16)
            rms = float(np.sqrt(np.mean(np.square(buf, dtype=np.float64)))) if buf.size else 0.0
            log.info("session=%s finalize: %d bytes pcm rms=%.0f peak=%.0f",
                     session_id, len(pcm), rms,
                     float(np.abs(buf).max()) if buf.size else 0.0)
            text = await asyncio.to_thread(call_asr, bytes(pcm), session_rate)
            log.info("session=%s turn: %d bytes pcm -> stt=%r",
                     session_id, len(pcm), text)
            if not text:
                return
            if not await ws_send(ws, {"session_id": session_id, "type": "stt", "text": text}):
                log.info("session=%s device ended before stt (%d pcm bytes)", session_id, len(pcm))
                return
            t0 = time.monotonic()
            try:
                kind, payload = await hermes_turn(session_id, device_id, text)
                if kind == "voice":
                    await _speak_audio(payload)
                else:
                    await _speak_text(payload)
            except Exception as e:
                log.warning("session=%s Hermes brain failed (%s); stateless gx10 fallback",
                            session_id, e)
                await _speak_text(await asyncio.to_thread(call_llm, text))
            log.info("session=%s turn total %.2fs brain=%s", session_id,
                     time.monotonic() - t0, "hermes" if _brain_connected else "gx10")
        except Exception as e:
            # never sever the board's socket on a model blip
            log.exception("session=%s turn failed: %s", session_id, e)
        finally:
            processing = False
            pcm.clear()
            had_speech = False
            silence_ms = 0

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                if pcm:
                    # A disconnect with a multi-minute buffer is ambient silence
                    # from a session that never VAD-stopped (no real utterance);
                    # ASRing it wastes the single whisper model and jams live
                    # turns (measured: 2 min of noise -> 20+s whisper). Cap it.
                    if len(pcm) / (session_rate * 2) <= 45:
                        try:
                            txt = await asyncio.to_thread(call_asr, bytes(pcm), session_rate)
                            log.info("session=%s disconnect with %d pcm bytes -> stt=%r",
                                     session_id, len(pcm), txt)
                        except Exception as e:
                            log.exception("session=%s disconnect-asr failed: %s", session_id, e)
                    else:
                        log.info("session=%s disconnect with %d pcm bytes; "
                                 "skipping ASR (long/silent buffer)",
                                 session_id, len(pcm))
                return
            if msg["type"] != "websocket.receive":
                continue
            if msg.get("text") is not None:
                try:
                    j = json.loads(msg["text"])
                except Exception:
                    continue
                t = j.get("type")
                if t == "hello":
                    configure(j.get("audio_params") or {})
                    # Prefer the board's own identity when the firmware reports it
                    # (hello "mac" is the stable chip MAC; device_id is a fw uuid).
                    dev_id = j.get("device_id") or j.get("mac")
                    if isinstance(dev_id, str) and dev_id.strip():
                        device_id = dev_id.strip()
                    entry = ACTIVE_SESSIONS.get(session_id)
                    if entry is not None:
                        entry["device_id"] = device_id
                    await ws_send(ws, {
                        "type": "hello", "transport": "websocket", "session_id": session_id,
                        "audio_params": {"format": "opus", "sample_rate": AUDIO_RATE,
                                         "channels": 1, "frame_duration": FRAME_MS},
                    })
                    log.info("session=%s hello -> replied rate=%d", session_id, session_rate)
                elif t == "listen":
                    if j.get("state") == "start":
                        speaking = True
                        frames_recv = 0
                        decode_warned = False
                        had_speech = False
                        silence_ms = 0
                        pcm.clear()
                        log.info("session=%s listen start (mode=%s)", session_id, j.get("mode"))
                    elif j.get("state") in ("stop", "detect"):
                        log.info("session=%s listen stop: %d opus frames, %d bytes pcm",
                                 session_id, frames_recv, len(pcm))
                        speaking = False
                        await finalize()
                elif t == "abort":
                    log.info("session=%s abort", session_id)
                    return
                continue
            data = msg.get("bytes")
            if data is not None and speaking and not processing:
                frames_recv += 1
                try:
                    frame = decoder.decode(bytes(data), ul_frame_samples)
                except Exception as e:
                    if not decode_warned:
                        log.warning("session=%s opus decode failed: %s", session_id, e)
                        decode_warned = True
                    continue
                pcm.extend(frame)
                if had_speech and silence_ms >= VAD_SILENCE_MS:
                    await finalize()
                else:
                    try:
                        energy = float(np.abs(np.frombuffer(frame, dtype=np.int16)).mean())
                    except Exception:
                        energy = 0.0
                    if energy > VAD_ENERGY:
                        had_speech = True
                        silence_ms = 0
                    elif had_speech:
                        silence_ms += frame_ms
    except websockets.exceptions.ConnectionClosed:
        pass


@app.websocket("/xiaozhi/v1/")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = os.urandom(8).hex()
    ACTIVE_SESSIONS[session_id] = {"ws": ws, "device_id": ""}
    log.info("device connected session=%s", session_id)
    try:
        await handle_conversation(ws, session_id)
    finally:
        ACTIVE_SESSIONS.pop(session_id, None)
        log.info("device disconnected session=%s", session_id)


@app.api_route("/xiaozhi/ota/", methods=["GET", "POST"])
async def ota_info():
    now = int(time.time() * 1000)
    return {
        "firmware": {"version": "2.4.2", "url": ""},  # == installed -> no self-update
        "websocket": {"url": WS_URL, "token": ""},
        "server_time": {"timestamp": now, "timezone_offset": 0},
        # no "activation" object -> device skips the /ota/activate handshake
    }


@app.post("/xiaozhi/ota/activate")
async def ota_activate():
    # Local server: accept any activation payload.
    return {"code": "local", "message": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


async def run_servers():
    import uvicorn

    # One event loop for both servers — the Hermes brain connection must live on
    # the same loop as the board WebSocket handler that awaits it.
    ota_srv = uvicorn.Server(uvicorn.Config(app, host=HOST, port=OTA_PORT, log_level="info"))
    ws_srv = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=WS_PORT, log_level="info", ws="websockets"))
    log.info("OTA  http://%s:%s/xiaozhi/ota/", LAN_IP, OTA_PORT)
    log.info("WS   %s", WS_URL)
    if HERMES_BRAIN_URL:
        asyncio.create_task(_brain_connect_loop())
    await asyncio.gather(ota_srv.serve(), ws_srv.serve())


if __name__ == "__main__":
    asyncio.run(run_servers())
