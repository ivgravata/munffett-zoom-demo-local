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
from websockets.legacy.server import WebSocketServerProtocol, serve
from websockets.legacy.client import connect

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

# Store active bots
active_bots: Dict[str, Dict[str, Any]] = {}

# Persona única: Munffett
personas = {
    "munffett": {
        "name": "Munffett",
        "instructions": """You are Munffett, a senior stock analyst with 80+ years of hard-earned judgment.
You specialize in Alphabet, Microsoft, Amazon, Meta, Mastercard, Danaher, Intuit,
GE Aerospace, Moody's, BTG Pactual, Localiza, Hims & Hers, and Nvidia—their sectors and competitors.
You've lived through long economic cycles; you were taught by Warren Buffett and Charlie Munger,
and you apply Chris Hohn's discipline. Use that mindset.

Identity rules:
• If asked "who are you?" or "what's your name?", always reply exactly: "I am Munffett."
• Never say you are an AI or assistant; never ask the user to name you.
• Stay in character at all times.

Style & voice:
• Calm, terse, evidence-driven. Prefer plain English; avoid jargon unless asked.
• Never refer to companies by ticker—use company names.
• Keep answers under ~10 seconds unless asked to go deeper.
• Detect Portuguese vs. English and reply in that language.
• If interrupted, stop immediately and listen.

Scope & behavior:
• You can discuss any company, but you are a true expert on the companies listed above.
• Prioritize conclusions and next actions; briefly reason aloud only when useful.
• No personalized investment advice; keep it educational/research-level.
• If unsure, say what you’d check next (10-K, investor day, transcripts, filings).

Zoom etiquette:
• Acknowledge new speakers briefly; don’t monologue.
• If audio is unclear, ask concisely for a repeat."""
    }
}


class RecallAPIClient:
    """Client for interacting with Recall.ai API."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://us-east-1.recall.ai/api/v1"
        
    async def create_bot(self, meeting_url: str, bot_name: str, persona_key: str) -> Dict[str, Any]:
        """Create a bot in Recall.ai."""
        
        backend_url = os.getenv("PUBLIC_URL")
        if not backend_url:
            raise ValueError("PUBLIC_URL environment variable is not set on Railway.")

        frontend_url = os.getenv("FRONTEND_URL")
        if not frontend_url:
            raise ValueError("FRONTEND_URL environment variable is not set on Railway.")

        ws_url = f"{backend_url.replace('https://', 'wss://').replace('http://', 'ws://')}/ws?persona={persona_key}"
        final_url = f"{frontend_url}?wss={ws_url}"
        
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "output_media": {
                "camera": {
                    "kind": "webpage",
                    "config": {"url": final_url}
                }
            },
            "variant": {"zoom": "web_4_core"}
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/bot",
                json=payload,
                headers={"Authorization": self.api_key, "Content-Type": "application/json"}
            ) as response:
                if response.status == 200 or response.status == 201:
                    data = await response.json()
                    logger.info(f"Bot created successfully: {data.get('id')}")
                    return data
                else:
                    text = await response.text()
                    logger.error(f"Failed to create bot: {response.status} - {text}")
                    raise Exception(f"Failed to create bot: {text}")
    
    async def end_bot(self, bot_id: str) -> Dict[str, Any]:
        """End a bot session in Recall.ai."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/bot/{bot_id}/leave_call",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"Failed to end bot: {await response.text()}")
    
    async def list_bots(self) -> list:
        """List all active bots."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/bot", headers={"Authorization": self.api_key}) as response:
                return await response.json() if response.status == 200 else []


async def connect_to_openai_with_persona(persona_key: str):
    """Connect to OpenAI's WebSocket endpoint with a specific persona."""
    uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    persona = personas.get(persona_key)

    if not persona:
        raise ValueError(f"Persona '{persona_key}' not found.")

    try:
        ws = await connect(
            uri,
            extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
            subprotocols=["realtime"],
        )
        logger.info(f"Successfully connected to OpenAI with persona: {persona_key}")

        response = await ws.recv()
        event = json.loads(response)
        if event.get("type") != "session.created":
            raise Exception(f"Expected session.created, got {event.get('type')}")
        
        update_session = {
            "type": "session.update",
            "session": {
                "instructions": persona["instructions"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "modalities": ["text", "audio"],
                "voice": "alloy",
                "turn_detection": {"type": "server_vad"}
            },
        }
        await ws.send(json.dumps(update_session))
        logger.info(f"Sent session.update message with {persona_key} persona")
        return ws, event

    except Exception as e:
        logger.error(f"Failed to connect to OpenAI: {str(e)}")
        raise


async def websocket_handler(request):
    """Handle WebSocket connections from Recall.ai bots."""
    ws = web.WebSocketResponse(protocols=["realtime"])
    await ws.prepare(request)
    
    persona_key = request.query.get('persona', 'munffett') # Default to munffett
    openai_ws = None
    
    try:
        openai_ws, session_created = await connect_to_openai_with_persona(persona_key)
        await ws.send_str(json.dumps(session_created))
        
        async def relay_to_openai():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await openai_ws.send(msg.data)
        
        async def relay_from_openai():
            async for msg in openai_ws:
                await ws.send_str(msg)

        await asyncio.gather(relay_to_openai(), relay_from_openai())
        
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}")
    finally:
        if openai_ws and not openai_ws.closed:
            await openai_ws.close()
        await ws.close()
    
    return ws


async def create_bot(request):
    """API endpoint to create a bot."""
    try:
        data = await request.json()
        meeting_url = data.get('meeting_url')
        persona_key = "munffett" # Always use munffett
        
        if not meeting_url:
            return web.json_response({'error': 'meeting_url is required'}, status=400)
        
        recall_client = RecallAPIClient(RECALL_API_KEY)
        persona = personas[persona_key]
        bot_data = await recall_client.create_bot(meeting_url, persona["name"], persona_key)
        
        active_bots[bot_data['id']] = {'id': bot_data['id'], 'status': 'active'}
        return web.json_response(bot_data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def end_bot(request):
    """API endpoint to end a bot."""
    try:
        bot_id = request.match_info.get('bot_id')
        recall_client = RecallAPIClient(RECALL_API_KEY)
        result = await recall_client.end_bot(bot_id)
        if bot_id in active_bots:
            active_bots[bot_id]['status'] = 'ended'
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def ping(request):
    """Health check endpoint."""
    return web.json_response({'ok': True})

def create_app():
    """Create the aiohttp application."""
    app = web.Application()
    
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/api/recall/create', create_bot)
    app.router.add_post('/api/recall/end/{bot_id}', end_bot)
    app.router.add_get('/api/recall/ping', ping)
    
    async def cors_middleware(app, handler):
        async def middleware_handler(request):
            response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
            return response
        return middleware_handler
    
    app.middlewares.append(cors_middleware)
    return app

if __name__ == '__main__':
    app = create_app()
    logger.info(f"Starting API server on port {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
