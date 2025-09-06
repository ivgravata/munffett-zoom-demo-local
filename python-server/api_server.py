# api_server.py
# ElevenLabs TTS streaming helper — no .env, reads os.environ only.

import os
from typing import AsyncGenerator, Optional
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel
from elevenlabs.client import AsyncElevenLabs
from elevenlabs import VoiceSettings

ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.environ.get("ELEVEN_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVEN_MODEL_ID = os.environ.get("ELEVEN_MODEL_ID", "eleven_multilingual_v2")
DEFAULT_OUTPUT = os.environ.get("ELEVEN_OUTPUT_FORMAT", "mp3_44100_128")  # or "pcm_16000"

if not ELEVEN_API_KEY:
    raise RuntimeError("Set ELEVENLABS_API_KEY in Railway Variables")

eleven = AsyncElevenLabs(api_key=ELEVEN_API_KEY)
app = FastAPI(title="Munffett TTS (ElevenLabs)")

class TTSPayload(BaseModel):
    text: str
    voice_id: Optional[str] = None
    model_id: Optional[str] = None
    output_format: Optional[str] = None
    speed: Optional[float] = 1.0
    stability: Optional[float] = 0.15
    similarity_boost: Optional[float] = 0.9
    use_speaker_boost: Optional[bool] = True

@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "ok"

def _content_type(fmt: str) -> str:
    if fmt.startswith("mp3"): return "audio/mpeg"
    if fmt.startswith("pcm"): return "audio/L16"
    if fmt.startswith("wav"): return "audio/wav"
    return "application/octet-stream"

async def _stream_tts(
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    speed: float,
    stability: float,
    similarity_boost: float,
    use_speaker_boost: bool,
) -> AsyncGenerator[bytes, None]:
    stream = await eleven.text_to_speech.stream(
        voice_id=voice_id,
        model_id=model_id,           # <- correct param (not "model")
        text=text,
        output_format=output_format,
        voice_settings=VoiceSettings(
            stability=stability,
            similarity_boost=similarity_boost,
            use_speaker_boost=use_speaker_boost,
            style=0.0,
            speed=speed or 1.0,
        ),
    )
    async for chunk in stream:
        if chunk:
            yield chunk

@app.get("/eleven/tts")
async def eleven_tts_get(
    text: str = Query(..., min_length=1),
    voice_id: Optional[str] = None,
    model_id: Optional[str] = None,
    output_format: Optional[str] = None,
):
    v = voice_id or ELEVEN_VOICE_ID
    m = model_id or ELEVEN_MODEL_ID
    fmt = output_format or DEFAULT_OUTPUT
    gen = _stream_tts(text, v, m, fmt, 1.0, 0.15, 0.9, True)
    headers = {"Cache-Control": "no-store", "X-Voice-Id": v, "X-Model-Id": m, "X-Format": fmt}
    return StreamingResponse(gen, media_type=_content_type(fmt), headers=headers)

@app.post("/eleven/tts")
async def eleven_tts_post(payload: TTSPayload):
    if not payload.text or not payload.text.strip():
        raise HTTPException(400, "field 'text' is required")
    v = payload.voice_id or ELEVEN_VOICE_ID
    m = payload.model_id or ELEVEN_MODEL_ID
    fmt = payload.output_format or DEFAULT_OUTPUT
    gen = _stream_tts(payload.text.strip(), v, m, fmt, payload.speed or 1.0,
                      payload.stability or 0.15, payload.similarity_boost or 0.9,
                      payload.use_speaker_boost if payload.use_speaker_boost is not None else True)
    headers = {"Cache-Control": "no-store", "X-Voice-Id": v, "X-Model-Id": m, "X-Format": fmt}
    return StreamingResponse(gen, media_type=_content_type(fmt), headers=headers)
