#!/usr/bin/env python3
"""Minimal XiaoZhi WebSocket voice bridge for the RoboDog board.

Implements just enough of the XiaoZhi communication protocol
(https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md) to give the
flashed XiaoZhi voice firmware a fully-local conversation loop:

  board --Opus/WebSocket--> bridge --PCM--> faster-whisper (ASR local)
                                --text--> gx10 vLLM (the Hermes brain model)
                                --text--> Kokoro-82M TTS (spark-5307:8004)
  bridge --Opus frames--> board speaker

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


async def ws_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception:
        # device closed the session mid-reply (user tapped the button) — normal
        return False


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

    async def finalize():
        """Transcribe buffered audio and stream a reply (realtime full-duplex)."""
        nonlocal pcm, processing, had_speech, silence_ms
        processing = True
        try:
            if not pcm:
                return
            text = await asyncio.to_thread(call_asr, bytes(pcm), session_rate)
            log.info("session=%s turn: %d bytes pcm -> stt=%r",
                     session_id, len(pcm), text)
            if not text:
                return
            if not await ws_send(ws, {"session_id": session_id, "type": "stt", "text": text}):
                log.info("session=%s device ended before stt (%d pcm bytes)", session_id, len(pcm))
                return
            reply = await asyncio.to_thread(call_llm, text)
            log.info("session=%s LLM reply=%r", session_id, reply)
            if not await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "start"}):
                log.info("session=%s device ended before tts start", session_id)
                return
            if not await ws_send(ws, {
                "session_id": session_id, "type": "tts", "state": "sentence_start", "text": reply,
            }):
                log.info("session=%s device ended before tts text", session_id)
                return
            try:
                audio = await asyncio.to_thread(call_tts, reply)
                pcm_buf = np.frombuffer(audio, dtype=np.int16).tobytes()
                for i in range(0, len(pcm_buf), d_frame_bytes):
                    chunk = pcm_buf[i:i + d_frame_bytes]
                    if len(chunk) < d_frame_bytes:
                        chunk = chunk + b"\x00" * (d_frame_bytes - len(chunk))
                    try:
                        await ws.send_bytes(encoder.encode(chunk, d_frame_samples))
                    except Exception:
                        log.info("session=%s device ended mid-tts (%d/%d bytes)",
                                 session_id, i, len(pcm_buf))
                        return
                    await asyncio.sleep(0.001)
            except Exception as e:
                log_response("TTS stream", "call_tts", e)
            await ws_send(ws, {"session_id": session_id, "type": "tts", "state": "stop"})
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
                    try:
                        txt = await asyncio.to_thread(call_asr, bytes(pcm), session_rate)
                        log.info("session=%s disconnect with %d pcm bytes -> stt=%r",
                                 session_id, len(pcm), txt)
                    except Exception as e:
                        log.exception("session=%s disconnect-asr failed: %s", session_id, e)
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
    log.info("device connected session=%s", session_id)
    try:
        await handle_conversation(ws, session_id)
    finally:
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


def main():
    import threading
    import uvicorn

    # HTTP (OTA) on OTA_PORT, WebSocket on WS_PORT — two servers, one process.
    ota_cfg = uvicorn.Config(app, host=HOST, port=OTA_PORT, log_level="info")
    ota_srv = uvicorn.Server(ota_cfg)

    def run_ota():
        ota_srv.run()

    threading.Thread(target=run_ota, daemon=True).start()
    log.info("OTA  http://%s:%s/xiaozhi/ota/", LAN_IP, OTA_PORT)
    log.info("WS   %s", WS_URL)

    ws_cfg = uvicorn.Config(app, host=HOST, port=WS_PORT, log_level="info", ws="websockets")
    return uvicorn.Server(ws_cfg)


if __name__ == "__main__":
    server = main()
    server.run()
