import asyncio
import json
import logging
import os
import base64
from datetime import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
import websockets
import aiohttp_cors
from elevenlabs.client import AsyncElevenLabs
from elevenlabs import VoiceSettings # Removido 'Voice' que não era necessário

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RECALL_API_KEY = os.getenv("RECALL_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")


if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY must be set in environment variables")
if not RECALL_API_KEY:
    raise ValueError("RECALL_API_KEY must be set in environment variables")
if not ELEVENLABS_API_KEY:
    raise ValueError("ELEVENLABS_API_KEY must be set in environment variables")


active_bots: Dict[str, Dict[str, Any]] = {}

personas = {
      "munffett": {
        "name": "Munffett",
        "instructions": """You are Munffett, a senior investor with a lifetime of experience in the markets.
You specialize in Alphabet, Microsoft, Amazon, Meta, Mastercard, Danaher, Intuit,
GE Aerospace, Moody's, BTG Pactual, Localiza, Hims & Hers, and Nvidia—their sectors and competitors.
You've seen many market cycles, and your philosophy is a blend of the long-term, business-focused principles of your mentors, Warren Buffett and Charlie Munger. You focus on understanding businesses, their competitive moats, and the power of compounding over time.

Identity rules:
• If asked "who are you?" or "what's your name?", always reply exactly: "I am Munffett."
• Never say you are an AI or assistant; never ask the user to name you.
• Stay in character at all times.

Style & voice:
• Wise, patient, and clear-spoken. Use simple analogies and plain English to explain complex ideas, much like a teacher would.
• Ground your insights in facts and business fundamentals.
• Never refer to companies by ticker—use company names.
• Be concise, but not abrupt. Get to the point, but take the time needed to explain it properly.
• Detect Portuguese vs. English and reply in that language.
• If interrupted, stop immediately and listen.
• When you speak in portuguese, you use an accent from Brazillian dub actors 
from the 1950s, and you refer to your name as "Mânfet"
• When you speak in english, you use an accent from the 1950s.


Scope & behavior:
• You can discuss any company, but you are a true expert on the companies listed above.
• Start with the key takeaway, but always explain your reasoning. Focus on the 'why' behind a business, not just the 'what'.
• No personalized investment advice; keep it educational, focusing on principles and business analysis.
• If you don't know something, say so. Explain what you’d need to read or check to find an answer (e.g., the 10-K, an earnings call transcript, etc.).

Zoom etiquette:
• Acknowledge new speakers briefly; don’t monologue.
• If audio is unclear, ask concisely for a repeat."""
      }
}

class RecallAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://us-west-2.recall.ai/api/v1"
        
    async def create_bot(self, meeting_url: str, bot_name: str, persona_key: str) -> Dict[str, Any]:
        backend_url = os.getenv("PUBLIC_URL")
        if not backend_url: raise ValueError("PUBLIC_URL environment variable is not set on Railway.")
        frontend_url = os.getenv("FRONTEND_URL")
        if not frontend_url: raise ValueError("FRONTEND_URL environment variable is not set on Railway.")
        ws_url = f"{backend_url.replace('https://', 'wss://')}/ws?persona={persona_key}"
        final_url = f"{frontend_url}?wss={ws_url}"
        
        payload = {
            "meeting_url": meeting_url, "bot_name": bot_name,
            "output_media": {"camera": {"kind": "webpage", "config": {"url": final_url}}},
            "variant": {"zoom": "web_4_core"}
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/bot", json=payload,
                headers={"Authorization": self.api_key, "Content-Type": "application/json"}
            ) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    logger.info(f"Bot created successfully: {data.get('id')}")
                    return data
                else:
                    text = await response.text()
                    logger.error(f"Failed to create bot: {response.status} - {text}")
                    raise Exception(f"Failed to create bot: {text}")

async def connect_to_openai_with_persona(persona_key: str):
    uri = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2025-08-28"
    persona = personas.get(persona_key)
    if not persona: raise ValueError(f"Persona '{persona_key}' not found.")
    try:
        ws = await websockets.connect(
            uri,
            extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
            subprotocols=["realtime"],
        )
        logger.info(f"Successfully connected to OpenAI with persona: {persona_key}")
        event = json.loads(await ws.recv())
        if event.get("type") != "session.created":
            raise Exception(f"Expected session.created, got {event.get('type')}")
        
        update_session = {
            "type": "session.update",
            "session": { "instructions": persona["instructions"], "input_audio_format": "pcm16", "modalities": ["text"], "turn_detection": {"type": "server_vad"} },
        }
        await ws.send(json.dumps(update_session))
        return ws, event
    except Exception as e:
        logger.error(f"Failed to connect to OpenAI: {str(e)}")
        raise

async def websocket_handler(request):
    ws = web.WebSocketResponse(protocols=["realtime"])
    await ws.prepare(request)
    raw_persona = request.query.get('persona', 'munffett')
    persona_key = raw_persona.split('?')[0]
    logger.info(f"WebSocket connection initiated with persona: {persona_key}")
    openai_ws = None
    try:
        openai_ws, session_created = await connect_to_openai_with_persona(persona_key)
        await ws.send_str(json.dumps(session_created))
        elevenlabs_client = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)

        async def relay_to_openai():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(msg.data)
                    if event.get("type") == "session.update" and "session" in event:
                        session = event["session"]
                        session.pop("instructions", None); session.pop("modalities", None); session.pop("voice", None); session.pop("output_audio_format", None)
                    if not openai_ws.closed: await openai_ws.send(json.dumps(event))
                elif msg.type == aiohttp.WSMsgType.ERROR: break

                async def relay_from_openai():
            async for msg in openai_ws:
                if ws.closed:
                    break
                try:
                    data = json.loads(msg)

                    # 1) Pegamos texto incremental do Realtime
                    if data.get("type") == "response.text.delta":
                        text_chunk = data.get("delta")
                        item_id = data.get("item_id")

                        if text_chunk and item_id:
                            # 2) Stream TTS ElevenLabs corretamente (model_id/voice_id/output_format)
                            audio_stream = await elevenlabs_client.text_to_speech.stream(
                                text=text_chunk,
                                voice_id="jn34bTlmmOgOJU9XfPuy",  # sua voz
                                model_id="eleven_multilingual_v2",
                                output_format="pcm_16000",
                                voice_settings=VoiceSettings(
                                    stability=0.71,
                                    similarity_boost=0.5,
                                    style=0.0,
                                    use_speaker_boost=True,
                                ),
                            )
                            # 3) Envia os chunks como base64 num evento que o seu front/bridge entende
                            async for chunk in audio_stream:
                                if not chunk:
                                    continue
                                audio_event = {
                                    "type": "conversation.item.updated",
                                    "item": {
                                        "id": item_id,
                                        "delta": {
                                            "audio": base64.b64encode(chunk).decode("utf-8")
                                        },
                                    },
                                }
                                await ws.send_str(json.dumps(audio_event))
                    else:
                        # repassa outros eventos
                        await ws.send_str(msg)

                except Exception as e:
                    logger.error(f"ERROR in relay_from_openai: {e}", exc_info=True)


        await asyncio.gather(relay_to_openai(), relay_from_openai())
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}")
    finally:
        if openai_ws and not openai_ws.closed: await openai_ws.close()
        if not ws.closed: await ws.close()
    return ws

async def create_bot(request):
    try:
        data = await request.json()
        meeting_url = data.get('meeting_url')
        if not meeting_url: return web.json_response({'error': 'meeting_url is required'}, status=400)
        persona_key = "munffett"
        recall_client = RecallAPIClient(RECALL_API_KEY)
        bot_data = await recall_client.create_bot(meeting_url, personas[persona_key]["name"], persona_key)
        active_bots[bot_data['id']] = {'id': bot_data['id'], 'status': 'active'}
        return web.json_response(bot_data)
    except Exception as e:
        logger.error(f"Error creating bot: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def ping(request):
    return web.json_response({'ok': True})

def create_app():
    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={ "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*") })
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/api/recall/create', create_bot)
    app.router.add_get('/api/recall/ping', ping)
    for route in list(app.router.routes()): cors.add(route)
    return app

if __name__ == '__main__':
    app = create_app()
    logger.info(f"Starting API server on port {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
