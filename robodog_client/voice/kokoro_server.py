"""Kokoro-82M TTS server (24 kHz WAV, OpenAI-compatible /v1/audio/speech)."""
import io
import wave
import numpy as np
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from kokoro import KPipeline

VOICE = "af_heart"
LANG = "a"

print("Loading Kokoro pipeline...", flush=True)
pl = KPipeline(lang_code=LANG, repo_id="hexgrad/Kokoro-82M")
print("Kokoro ready.", flush=True)

app = FastAPI(title="Kokoro TTS")


def to_np(x):
    if hasattr(x, "cpu"):            # torch tensor (this venv has torch)
        x = x.detach().cpu()
    return np.asarray(x, dtype=np.float32)


class TTSReq(BaseModel):
    model: str = "kokoro-82m"
    input: str
    voice: str = VOICE


@app.post("/v1/audio/speech")
def tts(req: TTSReq):
    voice = req.voice or VOICE
    try:
        chunks = [to_np(g.audio) for g in pl(voice=voice, text=req.input)]
    except Exception:
        chunks = [to_np(g.audio) for g in pl(voice=VOICE, text=req.input)]
    a = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    s16 = (np.clip(a * 32767.0, -32768.0, 32767.0)).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(s16.tobytes())
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)
