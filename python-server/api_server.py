import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
import websockets
import aiohttp_cors

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RECALL_API_KEY = os.getenv("RECALL_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY must be set in environment variables")
if not RECALL_API_KEY:
    raise ValueError("RECALL_API_KEY must be set in environment variables")

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
    # Modelo atualizado conforme sua solicitação
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
            "session": {
                "instructions": persona["instructions"], "input_audio_format": "pcm16",
                "output_audio_format": "pcm16", "modalities": ["text", "audio"],
                "voice": {"id": "ash"},  # Corrected format
                "turn_detection": {"type": "server_vad"}
            },
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
        
        # Lógica de relay com o "guardião" da persona
        async def relay_to_openai():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(msg.data)
                    # SOLUÇÃO: Impede que o cliente sobrescreva as instruções
                    if event.get("type") == "session.update" and "session" in event:
                        if "instructions" in event["session"]:
                            del event["session"]["instructions"]
                        if "voice" in event["session"]:
                            del event["session"]["voice"]
                        if "model" in event["session"]:
                            del event["session"]["model"]

                    
                    if not openai_ws.closed:
                        await openai_ws.send(json.dumps(event))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

        async def relay_from_openai():
            async for msg in openai_ws:
                if not ws.closed:
                    await ws.send_str(msg)

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
    
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True, expose_headers="*",
                allow_headers="*", allow_methods="*")
    })

    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/api/recall/create', create_bot)
    app.router.add_get('/api/recall/ping', ping)

    for route in list(app.router.routes()):
        cors.add(route)
        
    return app

if __name__ == '__main__':
    app = create_app()
    logger.info(f"Starting API server on port {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
