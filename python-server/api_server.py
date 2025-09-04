import asyncio
import json
import logging
import os
import base64
import re
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
import websockets
import aiohttp_cors
from elevenlabs.client import AsyncElevenLabs
from elevenlabs import Voice, VoiceSettings

# --- Basic Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

# --- API Key Configuration ---
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RECALL_API_KEY = os.getenv("RECALL_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

if not OPENAI_API_KEY: raise ValueError("OPENAI_API_KEY must be set.")
if not RECALL_API_KEY: raise ValueError("RECALL_API_KEY must be set.")
if not ELEVENLABS_API_KEY: raise ValueError("ELEVENLABS_API_KEY must be set.")

# --- ElevenLabs Client Initialization ---
elevenlabs_client = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)
active_bots = {}
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
• When you speak in portuguese, you use an accent from Brazillian dub actors from the 1950s, and you refer to your name as "Mânfet"
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

# --- Recall.ai Bot Creation Logic (Unchanged) ---
class RecallAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://us-west-2.recall.ai/api/v1"
        
    async def create_bot(self, meeting_url: str, bot_name: str, persona_key: str):
        backend_url = os.getenv("PUBLIC_URL")
        if not backend_url: raise ValueError("PUBLIC_URL environment variable is not set.")
        frontend_url = os.getenv("FRONTEND_URL")
        if not frontend_url: raise ValueError("FRONTEND_URL environment variable is not set.")
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
                if response.status in [200, 201]: return await response.json()
                else:
                    text = await response.text()
                    logger.error(f"Failed to create bot: {response.status} - {text}")
                    raise Exception(f"Failed to create bot: {text}")

# --- MODIFIED: Connect to OpenAI requesting TEXT ONLY output ---
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
        event = json.loads(await ws.recv())
        if event.get("type") != "session.created":
            raise Exception(f"Expected session.created, got {event.get('type')}")
        
        update_session = {
            "type": "session.update",
            "session": {
                "instructions": persona["instructions"],
                "input_audio_format": "pcm16",
                "output_modalities": ["text"], # Request text only
                "turn_detection": {"type": "server_vad"}
            },
        }
        await ws.send(json.dumps(update_session))
        return ws, event
    except Exception as e:
        logger.error(f"Failed to connect to OpenAI: {str(e)}")
        raise

# --- NEW: Function to stream text to ElevenLabs and resulting audio to client ---
async def stream_audio_from_elevenlabs(text: str, client_ws, item_id: str):
    if not text: return
    logger.info(f"Streaming to ElevenLabs: '{text}'")
    try:
        audio_stream = await elevenlabs_client.generate(
            text=text,
            voice=Voice(
                voice_id="jn34bTlmmOgOJU9XfPuy",
                settings=VoiceSettings(stability=0.5, similarity_boost=0.75)
            ),
            model="eleven_turbo_v2",
            stream=True,
            output_format="pcm_24000"
        )
        async for audio_chunk in audio_stream:
            if audio_chunk and not client_ws.closed:
                payload = {
                    "type": "conversation.item.delta",
                    "delta": {
                        "type": "audio",
                        "audio": base64.b64encode(audio_chunk).decode('utf-8')
                    },
                    "item": {"id": item_id}
                }
                await client_ws.send_str(json.dumps(payload))
    except Exception as e:
        logger.error(f"Error streaming from ElevenLabs: {e}")

# --- NEW: Logic to process OpenAI text and pipe to ElevenLabs ---
async def process_openai_to_elevenlabs(openai_ws, client_ws):
    text_buffer = ""
    current_item_id = None
    async for msg in openai_ws:
        try:
            event = json.loads(msg)
            event_type = event.get("type")

            if event_type == "conversation.item.delta" and event.get("delta", {}).get("type") == "text":
                text_chunk = event["delta"]["content"]
                text_buffer += text_chunk
                current_item_id = event["item"]["id"]
                
                if any(char in text_chunk for char in ".!?"):
                    parts = re.split(r'([.!?])', text_buffer)
                    sentence = "".join(parts[:-1]).strip()
                    text_buffer = parts[-1]
                    if sentence:
                        await stream_audio_from_elevenlabs(sentence, client_ws, current_item_id)
            
            elif event_type == "conversation.item.updated" and event.get("item", {}).get("status") == "completed":
                if text_buffer.strip():
                    await stream_audio_from_elevenlabs(text_buffer.strip(), client_ws, current_item_id)
                    text_buffer = ""
                await client_ws.send_str(msg)
            
            else:
                if not client_ws.closed:
                    await client_ws.send_str(msg)
        except Exception as e:
            logger.error(f"Error processing message: {e}")

# --- MODIFIED: WebSocket handler to use the new processing logic ---
async def websocket_handler(request):
    ws = web.WebSocketResponse(protocols=["realtime"])
    await ws.prepare(request)
    persona_key = request.query.get('persona', 'munffett').split('?')[0]
    logger.info(f"WebSocket connection initiated with persona: {persona_key}")
    openai_ws = None
    try:
        openai_ws, session_created = await connect_to_openai_with_persona(persona_key)
        await ws.send_str(json.dumps(session_created))
        
        async def relay_to_openai():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(msg.data)
                    if event.get("type") == "session.update" and "session" in event:
                        # Prevent client from overriding key settings
                        event["session"].pop("instructions", None)
                        event["session"].pop("voice", None)
                        event["session"].pop("model", None)
                        event["session"].pop("output_modalities", None)
                    if not openai_ws.closed:
                        await openai_ws.send(json.dumps(event))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        
        await asyncio.gather(
            relay_to_openai(),
            process_openai_to_elevenlabs(openai_ws, ws)
        )
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}", exc_info=True)
    finally:
        if openai_ws and not openai_ws.closed: await openai_ws.close()
        if not ws.closed: await ws.close()
    return ws

# --- API Endpoints (Unchanged) ---
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

async def ping(request): return web.json_response({'ok': True})

# --- App Factory (Unchanged) ---
def create_app():
    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(
        allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")})
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/api/recall/create', create_bot)
    app.router.add_get('/api/recall/ping', ping)
    for route in list(app.router.routes()): cors.add(route)
    return app

if __name__ == '__main__':
    app = create_app()
    logger.info(f"Starting API server on port {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
