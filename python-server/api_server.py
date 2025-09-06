# api_server.py
# Servidor mínimo para TTS via ElevenLabs (streaming) – útil p/ testes locais.
# Requer: pip install fastapi uvicorn elevenlabs>=1.0.0 python-dotenv

import os
import asyncio
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# ElevenLabs SDK (usar nomes corretos: model_id / voice_id)
from elevenlabs.client import AsyncElevenLabs
from elevenlabs import VoiceSettings

load_dotenv()

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVEN_MODEL_ID = os.getenv("ELEVEN_MODEL_ID", "eleven_multilingual_v2")
DEFAULT_OUTPUT = os.getenv("ELEVEN_OUTPUT_FORMAT", "mp3_44100_128")  # ou "pcm_16000"

if not ELEVEN_API_KEY:
    raise RuntimeError("Faltou ELEVENLABS_API_KEY no .env")

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
    if fmt.startswith("mp3"):
        return "audio/mpeg"
    if fmt.startswith("pcm"):
        # raw 16-bit little-endian mono
        return "audio/L16"
    if fmt.startswith("wav"):
        return "audio/wav"
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
    """
    Gera áudio em streaming usando parâmetros CORRETOS da SDK:
    - voice_id (não "voice")
    - model_id (não "model")
    - output_format (ex.: mp3_44100_128 ou pcm_16000)
    """
    stream = await eleven.text_to_speech.stream(
        voice_id=voice_id,
        model_id=model_id,
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

    gen = _stream_tts(
        text=text,
        voice_id=v,
        model_id=m,
        output_format=fmt,
        speed=1.0,
        stability=0.15,
        similarity_boost=0.9,
        use_speaker_boost=True,
    )
    headers = {
        "Cache-Control": "no-store",
        "X-Voice-Id": v,
        "X-Model-Id": m,
        "X-Format": fmt,
    }
    return StreamingResponse(gen, media_type=_content_type(fmt), headers=headers)

@app.post("/eleven/tts")
async def eleven_tts_post(payload: TTSPayload):
    if not payload.text or not payload.text.strip():
        raise HTTPException(400, "field 'text' is required")
    v = payload.voice_id or ELEVEN_VOICE_ID
    m = payload.model_id or ELEVEN_MODEL_ID
    fmt = payload.output_format or DEFAULT_OUTPUT
    gen = _stream_tts(
        text=payload.text.strip(),
        voice_id=v,
        model_id=m,
        output_format=fmt,
        speed=payload.speed or 1.0,
        stability=payload.stability or 0.15,
        similarity_boost=payload.similarity_boost or 0.9,
        use_speaker_boost=payload.use_speaker_boost if payload.use_speaker_boost is not None else True,
    )
    headers = {
        "Cache-Control": "no-store",
        "X-Voice-Id": v,
        "X-Model-Id": m,
        "X-Format": fmt,
    }
    return StreamingResponse(gen, media_type=_content_type(fmt), headers=headers)

# Execução local:
# uvicorn api_server:app --host 0.0.0.0 --port 8081
