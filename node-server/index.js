// index.js
// Node 18+ recomendado. Instale deps: npm i express ws axios dotenv
// Se já usa outro framework no repo, mantenha as rotas e substitua a lógica abaixo.

require('dotenv').config();

const http = require('http');
const express = require('express');
const axios = require('axios');
const WebSocket = require('ws');
const { URL } = require('url');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// --- Config OpenAI Realtime ---
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const MODEL_REALTIME = process.env.MODEL_REALTIME || 'gpt-4o-realtime-preview-2025-05-08';

// --- Voice-swap (ElevenLabs) ---
const VOICE_SWAP = (process.env.VOICE_SWAP || 'elevenlabs').toLowerCase() === 'elevenlabs';
const ELEVEN_API_KEY = process.env.ELEVENLABS_API_KEY || '';
const ELEVEN_VOICE_ID = process.env.ELEVEN_VOICE_ID || 'JBFqnCBsd6RMkjVDRZzb';
const ELEVEN_MODEL_ID = process.env.ELEVEN_MODEL_ID || 'eleven_multilingual_v2';

// --- Recall Output Audio ---
const RECALL_API_KEY = process.env.RECALL_API_KEY || '';
// Opcional: defina RECALL_BOT_ID aqui ou passe ?bot_id= na conexão WS
const RECALL_BOT_ID_DEFAULT = process.env.RECALL_BOT_ID || '';

if (!OPENAI_API_KEY) {
  console.error('Faltou OPENAI_API_KEY no .env');
  process.exit(1);
}

if (VOICE_SWAP && (!ELEVEN_API_KEY || !RECALL_API_KEY)) {
  console.error('VOICE_SWAP=elevenlabs requer ELEVENLABS_API_KEY e RECALL_API_KEY');
  process.exit(1);
}

// Health
app.get('/health', (_, res) => res.status(200).send('ok'));

// --- Página de depuração (/player) e WS auxiliar (/ws) ---
const players = new Set();
const server = http.createServer(app);

const wssPlayers = new WebSocket.Server({ server, path: '/ws' });
wssPlayers.on('connection', (ws) => {
  players.add(ws);
  ws.on('close', () => players.delete(ws));
});
function broadcastSay(text) {
  const msg = JSON.stringify({ type: 'say', text });
  for (const ws of players) {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  }
}

// Página simples que toca qualquer texto empurrado por /ws (debug fora do Zoom)
app.get('/player', (_, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html>
<html><head><meta charset="utf-8"><title>Munffett Player</title>
<style>body{font:16px system-ui;background:#0b0b0c;color:#eaeaea;padding:24px}#log{white-space:pre-wrap;background:#111;padding:12px;border-radius:12px;max-height:45vh;overflow:auto}</style>
</head><body>
<h2>Munffett Player</h2>
<p>Recebe <code>say</code> via WebSocket e toca TTS do ElevenLabs (para depurar localmente).</p>
<div id="log"></div>
<audio id="player" autoplay></audio>
<script>
const log = (m)=>{const el=document.getElementById('log'); el.textContent += m + "\\n"; el.scrollTop = el.scrollHeight;};
const wsProto = location.protocol==='https:'?'wss:':'ws:';
const ws = new WebSocket(wsProto+'//'+location.host+'/ws');
ws.onopen = ()=>log('WS conectado');
ws.onmessage = async (ev)=>{
  try{
    const msg = JSON.parse(ev.data);
    if(msg.type==='say' && msg.text){
      log('▶︎ ' + (msg.text.length>140?msg.text.slice(0,140)+'…':msg.text));
      const u = new URL('/eleven/tts', location.origin);
      u.searchParams.set('text', msg.text);
      const audio = document.getElementById('player');
      audio.src = u.toString()+'&t='+Date.now();
      await audio.play();
    }
  }catch(e){log('erro: '+e.message)}
};
</script>
</body></html>`);
});

// Endpoint que faz proxy do stream ElevenLabs → browser (debug/local)
app.get('/eleven/tts', async (req, res) => {
  try {
    const text = (req.query.text || '').toString();
    if (!text) return res.status(400).send('missing ?text');

    const url = `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(ELEVEN_VOICE_ID)}/stream`;
    const body = {
      text,
      model_id: ELEVEN_MODEL_ID,      // <- nome correto (não use "model")
      output_format: 'mp3_44100_128', // fácil de tocar no browser
      voice_settings: {
        stability: 0.15,
        similarity_boost: 0.9,
        use_speaker_boost: true,
        style: 0.0,
        speed: 1.0
      }
    };

    const resp = await axios.post(url, body, {
      headers: {
        'xi-api-key': ELEVEN_API_KEY,
        'Content-Type': 'application/json',
        'Accept': 'audio/mpeg'
      },
      responseType: 'stream',
      timeout: 60000
    });

    res.setHeader('Content-Type', 'audio/mpeg');
    res.setHeader('Cache-Control', 'no-store');
    resp.data.pipe(res);
  } catch (err) {
    console.error('[eleven/tts] error', err?.response?.status, err?.message);
    res.status(500).end();
  }
});

// Função utilitária: sintetiza MP3 (buffer → base64)
async function elevenSynthesizeMp3Base64(text) {
  const url = `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(ELEVEN_VOICE_ID)}/stream`;
  const body = {
    text,
    model_id: ELEVEN_MODEL_ID,
    output_format: 'mp3_44100_128'
  };
  const resp = await axios.post(url, body, {
    headers: { 'xi-api-key': ELEVEN_API_KEY, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg' },
    responseType: 'arraybuffer',
    timeout: 60000
  });
  const b64 = Buffer.from(resp.data).toString('base64');
  return b64;
}

// Envia MP3 para o Zoom via Recall Output Audio
async function recallOutputAudio(botId, base64Mp3) {
  if (!botId) throw new Error('botId ausente (defina RECALL_BOT_ID ou passe ?bot_id= na conexão)');
  const url = `https://us-east-1.recall.ai/api/v1/bot/${encodeURIComponent(botId)}/output_audio/`;
  await axios.post(url, { kind: 'mp3', b64_data: base64Mp3 }, {
    headers: { 'Authorization': RECALL_API_KEY, 'Content-Type': 'application/json' },
    timeout: 30000
  });
}

// -------------------- Bridge Realtime <-> (seu front/Recall) --------------------
const wssBridge = new WebSocket.Server({ server, path: '/recall' });

wssBridge.on('connection', (downstream, req) => {
  // Você pode passar ?bot_id=... na URL do WS para escolher o bot que vai tocar o áudio.
  const url = new URL(req.url, `http://${req.headers.host}`);
  const RATE = Number(url.searchParams.get('rate') || 16000);
  const BOT_ID = url.searchParams.get('bot_id') || RECALL_BOT_ID_DEFAULT;

  const upstream = new WebSocket(
    `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(MODEL_REALTIME)}`,
    {
      headers: {
        Authorization: `Bearer ${OPENAI_API_KEY}`,
        'OpenAI-Beta': 'realtime=v1'
      }
    }
  );

  let upstreamOpen = false;
  let downstreamOpen = true;

  // Acumula texto por resposta
  const textBuffers = new Map(); // response_id -> string

  upstream.on('open', () => {
    upstreamOpen = true;

    // Sessão Realtime sem áudio de saída do OpenAI (vamos usar ElevenLabs)
    const session = {
      model: MODEL_REALTIME,
      turn_detection: { type: 'server_vad', threshold: 0.5, prefix_padding_ms: 250, silence_duration_ms: 700 },
      input_audio_format: { type: 'pcm16', sample_rate_hz: RATE },
      // Nada de voice/output_audio_format: queremos apenas TEXTO
    };
    upstream.send(JSON.stringify({ type: 'session.update', session }));
  });

  upstream.on('message', async (buf) => {
    if (!downstreamOpen) return;

    // Encaminhe tudo ao cliente (útil p/ logs/debug do front)
    try { downstream.send(buf); } catch {}

    // Voice-swap: intercepta texto e toca com ElevenLabs → Zoom (Recall)
    if (!VOICE_SWAP) return;

    try {
      const ev = JSON.parse(buf.toString());
      const t = ev?.type;

      if (t === 'response.text.delta') {
        const rid = ev.response_id || ev.item_id || 'default';
        const prev = textBuffers.get(rid) || '';
        textBuffers.set(rid, prev + (ev.delta || ''));
      }

      if (t === 'response.text.done' || t === 'response.done') {
        const rid = ev.response_id || ev.item_id || 'default';
        const fullText = (textBuffers.get(rid) || '').trim();
        textBuffers.delete(rid);

        if (fullText) {
          // a) injeta no Zoom via Recall Output Audio (oficial)
          try {
            const b64 = await elevenSynthesizeMp3Base64(fullText);
            await recallOutputAudio(BOT_ID, b64);  // Bot precisa ter sido criado com automatic_audio_output (pode ser mp3 silencioso). 
          } catch (e) {
            console.error('[recallOutputAudio] erro', e?.response?.status, e?.message);
          }

          // b) (opcional) também manda p/ /player (debug local)
          try { broadcastSay(fullText); } catch {}
        }
      }
    } catch { /* ignore frames não-JSON */ }
  });

  // Tudo que vier do cliente, repasse ao OpenAI (ex.: audio input, response.create etc.)
  downstream.on('message', (msg) => {
    if (upstreamOpen) upstream.send(msg);
  });

  downstream.on('close', () => {
    downstreamOpen = false;
    try { upstream.close(); } catch {}
  });
  downstream.on('error', () => {
    downstreamOpen = false;
    try { upstream.close(); } catch {}
  });

  upstream.on('close', () => {
    if (downstreamOpen) try { downstream.close(); } catch {}
  });
  upstream.on('error', (e) => {
    console.error('[Realtime error]', e?.message);
    if (downstreamOpen) try { downstream.close(); } catch {}
  });
});

// --------------------------------------------------------------------------------

server.listen(PORT, () => {
  console.log(`[Server] Listening on :${PORT}`);
  console.log(`VOICE_SWAP=${VOICE_SWAP ? 'elevenlabs' : 'off'}`);
});
