import asyncio
import json
import logging
import os
import base64
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
import websockets
import aiohttp_cors

# --- Configuração Básica ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

# --- Configuração das Chaves de API a partir das Variáveis de Ambiente ---
PORT = int(os.getenv("PORT", 8000))
RECALL_API_KEY = os.getenv("RECALL_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")

if not RECALL_API_KEY: raise ValueError("A variável de ambiente RECALL_API_KEY tem de estar definida.")
if not ELEVENLABS_API_KEY: raise ValueError("A variável de ambiente ELEVENLABS_API_KEY tem de estar definida.")
if not ELEVENLABS_AGENT_ID: raise ValueError("A variável de ambiente ELEVENLABS_AGENT_ID tem de estar definida.")

# --- Informação do Bot (Simplificado) ---
active_bots = {}
personas = { "munffett": { "name": "Munffett" } }

# --- Lógica de Criação do Bot Recall.ai (Sem alterações) ---
class RecallAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://us-west-2.recall.ai/api/v1"
        
    async def create_bot(self, meeting_url: str, bot_name: str, persona_key: str):
        backend_url = os.getenv("PUBLIC_URL")
        if not backend_url: raise ValueError("A variável de ambiente PUBLIC_URL não está definida na Railway.")
        frontend_url = os.getenv("FRONTEND_URL")
        if not frontend_url: raise ValueError("A variável de ambiente FRONTEND_URL não está definida na Railway.")
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
                    logger.error(f"Falha ao criar o bot: {response.status} - {text}")
                    raise Exception(f"Falha ao criar o bot: {text}")

# --- Lógica para gerir o stream de áudio bidirecional com o Agente ElevenLabs ---
async def handle_elevenlabs_agent_stream(client_ws):
    uri = f"wss://api.elevenlabs.io/v1/agent/{ELEVENLABS_AGENT_ID}/stream"
    
    async with websockets.connect(uri) as elevenlabs_ws:
        # 1. Envia a mensagem de autenticação primeiro
        auth_message = {
            "xi_api_key": ELEVENLABS_API_KEY,
            "voice_settings": { "stability": 0.5, "similarity_boost": 0.7 }
        }
        await elevenlabs_ws.send(json.dumps(auth_message))

        async def forward_to_elevenlabs():
            # Encaminha o áudio do Cliente (Zoom) -> ElevenLabs
            try:
                async for msg in client_ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await elevenlabs_ws.send(msg.data)
            except Exception as e:
                logger.info(f"Ligação do cliente fechada: {e}")

        async def forward_to_client():
            # Encaminha o áudio da ElevenLabs -> Cliente (Zoom)
            try:
                async for audio_chunk in elevenlabs_ws:
                    if not client_ws.closed:
                        payload = {
                            "type": "conversation.item.delta",
                            "delta": {
                                "type": "audio",
                                "audio": base64.b64encode(audio_chunk).decode('utf-8')
                            },
                            "item": {"id": "elevenlabs_stream"}
                        }
                        await client_ws.send_str(json.dumps(payload))
            except Exception as e:
                logger.info(f"Ligação da ElevenLabs fechada: {e}")

        # Executa ambas as tarefas em simultâneo
        await asyncio.gather(forward_to_elevenlabs(), forward_to_client())

# --- Gestor de WebSocket principal que agora usa a lógica do Agente ElevenLabs ---
async def websocket_handler(request):
    ws = web.WebSocketResponse(protocols=["realtime"])
    await ws.prepare(request)
    logger.info("Ligação WebSocket iniciada, a conectar ao Agente ElevenLabs...")
    
    try:
        await handle_elevenlabs_agent_stream(ws)
    except Exception as e:
        logger.error(f"Erro no gestor de WebSocket: {e}", exc_info=True)
    finally:
        if not ws.closed:
            await ws.close()
    return ws

# --- Endpoints da API (Sem alterações) ---
async def create_bot(request):
    try:
        data = await request.json()
        meeting_url = data.get('meeting_url')
        if not meeting_url: return web.json_response({'error': 'meeting_url é obrigatório'}, status=400)
        persona_key = "munffett"
        recall_client = RecallAPIClient(RECALL_API_KEY)
        bot_data = await recall_client.create_bot(meeting_url, personas[persona_key]["name"], persona_key)
        active_bots[bot_data['id']] = {'id': bot_data['id'], 'status': 'active'}
        return web.json_response(bot_data)
    except Exception as e:
        logger.error(f"Erro ao criar o bot: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def ping(request): return web.json_response({'ok': True})

# --- Fábrica da Aplicação (Sem alterações) ---
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
    logger.info(f"A iniciar o servidor da API na porta {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
