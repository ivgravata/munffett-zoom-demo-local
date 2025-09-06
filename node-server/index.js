// index.js
// OpenAI Realtime ↔ Recall bridge with optional ElevenLabs voice-swap
// Reads ONLY from process.env (Railway Variables). No .env file required.

const http = require('http');
const express = require('express');
const axios = require('axios');
const WebSocket = require('ws');

const app = express();
app.use(express.json());

const PORT = Number(process.env.PORT || 3000);

// ---------- Core env ----------
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || '';
const MODEL_REALTIME =
  process.env.MODEL_REALTIME || 'gpt-4o-realtime-preview-2025-05-08';

// ---------- Recall (required for /create-bot) ----------
const RECALL_API_KEY = process.env.RECALL_API_KEY || '';
const RECALL_REGION =
  process.env.RECALL_REGION || 'us-east-1'; // match your Recall account region
const RECALL_BASE = `https://${RECALL_REGION}.recall.ai/api/v1`;

// ---------- Voice-swap / output path ----------
let VOICE_SWAP = (process.env.VOICE_SWAP || 'elevenlabs').toLowerCase();
const PLAY_TO = (process.env.PLAY_TO || 'recall').toLowerCase(); // 'recall' (output_audio) or 'playUrl'

// ---------- ElevenLabs (optional) ----------
const ELEVEN_API_KEY = process.env.ELEVENLABS_API_KEY || '';
const ELEVEN_VOICE_ID =
  process.env.ELEVEN_VOICE_ID || 'JBFqnCBsd6RMkjVDRZzb';
const ELEVEN_MODEL_ID =
  process.env.ELEVEN_MODEL_ID || 'eleven_multilingual_v2';

// ---------- Optional public URL (only if you plan to use Output Media debug page) ----------
const PUBLIC_BASE_URL = process.env.PUBLIC_BASE_URL || ''; // e.g. https://your-app.up.railway.app

// ---- Soft validation (do NOT crash the container) ----
if (!OPENAI_API_KEY) {
  console.warn(
    '[WARN] OPENAI_API_KEY not set. Realtime bridge will not function, but /health and /create-bot will still respond.'
  );
}
if (!RECALL_API_KEY) {
  console.warn(
    '[WARN] RECALL_API_KEY not set. /create-bot will return a 500 until you add it.'
  );
}
if (VOICE_SWAP === 'elevenlabs' && !ELEVEN_API_KEY) {
  console.warn(
    '[WARN] VOICE_SWAP=elevenlabs but ELEVENLABS_API_KEY is missing. Disabling voice-swap.'
  );
  VOICE_SWAP = 'off';
}

// ---------- Health ----------
app.get('/health', (_, res) => res.status(200).send('ok'));

// ---------- Optional debug: /player + /ws (lets you hear the TTS in a browser) ----------
const players = new Set();
const server = http.createServer(app);
const wssPlayers = new WebSocket.Server({ server, path: '/ws' });
wssPlayers.on('connection', (ws) => {
  players.add(ws);
  ws.on('close', () => players.delete(ws));
});
function broadcastSay(text) {
  const msg = JSON.stringify({ type: 'say', text });
  for (const ws of players) if (ws.readyState === WebSocket.OPEN) ws.send(msg);
}
app.get('/player', (_, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html><html><head><meta charset="utf-8"><title>Munffett Player</title>
<style>body{font:16px system-ui;background:#0b0b0c;color:#eaeaea;padding:24px}#log{white-space:pre-wrap;background:#111;padding:12px;border-radius:12px;max-height:45vh;overflow:auto}</style>
</head><body><h2>Munffett Player</h2>
<p>Recebe <code>say</code> via WebSocket e toca TTS do ElevenLabs (debug fora do Zoom).</p>
<div id="log"></div><audio id="player" autoplay></audio>
<script>
const log = (m)=>{const el=document.getElementById('log'); el.textContent += m + "\\n"; el.scrollTop = el.scrollHeight;};
const wsProto = location.protocol==='https:'?'wss:':'ws:'; const ws = new WebSocket(wsProto+'//'+location.host+'/ws');
ws.onopen = ()=>log('WS conectado');
ws.onmessage = async (ev)=>{ try{ const msg = JSON.parse(ev.data); if(msg.type==='say'){ const u = new URL('/eleven/tts', location.origin); u.searchParams.set('text', msg.text); const audio = document.getElementById('player'); audio.src = u.toString()+'&t='+Date.now(); await audio.play(); } }catch(e){ log('erro: '+e.message) } };
</script></body></html>`);
});

// ---------- ElevenLabs helpers (HTTP streaming API) ----------
async function elevenMp3Base64(text) {
  if (!ELEVEN_API_KEY) throw new Error('ELEVENLABS_API_KEY missing');
  const url = `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(
    ELEVEN_VOICE_ID
  )}/stream`;
  const body = { text, model_id: ELEVEN_MODEL_ID, output_format: 'mp3_44100_128' };
  const resp = await axios.post(url, body, {
    headers: {
      'xi-api-key': ELEVEN_API_KEY,
      'Content-Type': 'application/json',
      Accept: 'audio/mpeg',
    },
    responseType: 'arraybuffer',
    timeout: 60000,
  });
  return Buffer.from(resp.data).toString('base64');
}

app.get('/eleven/tts', async (req, res) => {
  try {
    const text = (req.query.text || '').toString();
    if (!text) return res.status(400).send('missing ?text');
    const url = `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(
      ELEVEN_VOICE_ID
    )}/stream`;
    const body = { text, model_id: ELEVEN_MODEL_ID, output_format: 'mp3_44100_128' };
    const resp = await axios.post(url, body, {
      headers: {
        'xi-api-key': ELEVEN_API_KEY,
        'Content-Type': 'application/json',
        Accept: 'audio/mpeg',
      },
      responseType: 'stream',
      timeout: 60000,
    });
    res.setHeader('Content-Type', 'audio/mpeg');
    res.setHeader('Cache-Control', 'no-store');
    resp.data.pipe(res);
  } catch (e) {
    console.error('[GET /eleven/tts] error', e?.response?.status, e?.message);
    res.status(500).end();
  }
});

// ---------- Recall: create bot (this is what your Slack flow calls) ----------
app.post('/create-bot', async (req, res) => {
  try {
    if (!RECALL_API_KEY) {
      return res
        .status(500)
        .json({ ok: false, error: 'RECALL_API_KEY not set in Railway Variables' });
    }
    const meeting_url = (req.body?.meeting_url || '').trim();
    const bot_name = req.body?.bot_name || 'Munffett (Voice Swap)';
    if (!meeting_url) {
      return res.status(400).json({ ok: false, error: 'missing meeting_url' });
    }

    // Minimal payload to ensure the bot ENTERS the call.
    // (You can add transcript/recording/realtime_endpoints later.)
    const payload = {
      meeting_url,
      bot_name,
      // If you plan to stream a webpage (Method 1 Output Media), set this:
      // output_media: PUBLIC_BASE_URL ? { camera: { kind: 'webpage', config: { url: `${PUBLIC_BASE_URL}/player` } } } : undefined,
      // Variant with more CPU for smooth audio:
      variant: { zoom: 'web_4_core', google_meet: 'web_4_core', microsoft_teams: 'web_4_core' },
    };

    const r = await axios.post(`${RECALL_BASE}/bot/`, payload, {
      headers: { Authorization: RECALL_API_KEY, 'Content-Type': 'application/json' },
      timeout: 15000,
    });
    return res.status(201).json({ ok: true, bot: r.data });
  } catch (e) {
    console.error('[POST /create-bot] error', e?.response?.status, e?.message);
    // Surface Recall’s error payload if any, but always return 502 to Slack so it shows "Bad Gateway".
    return res.status(502).json({
      ok: false,
      where: 'recall',
      status: e?.response?.status || null,
      data: e?.response?.data || null,
      message: e?.message || 'unknown',
    });
  }
});

// ---------- Realtime bridge (optional; keep enabled only if OPENAI_API_KEY is set) ----------
const wssBridge = new WebSocket.Server({ server, path: '/recall' });
wssBridge.on('connection', (downstream, req) => {
  if (!OPENAI_API_KEY) {
    console.warn('[WS /recall] OPENAI_API_KEY missing; closing.');
    downstream.close();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host}`);
  const RATE = Number(url.searchParams.get('rate') || 16000);
  const upstream = new WebSocket(
    `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(MODEL_REALTIME)}`,
    { headers: { Authorization: `Bearer ${OPENAI_API_KEY}`, 'OpenAI-Beta': 'realtime=v1' } }
  );

  let upstreamOpen = false;
  let downstreamOpen = true;

  // buffer text by response id
  const textBuffers = new Map();

  upstream.on('open', () => {
    upstreamOpen = true;
    const session = {
      model: MODEL_REALTIME,
      turn_detection: { type: 'server_vad', threshold: 0.5, prefix_padding_ms: 250, silence_duration_ms: 700 },
      input_audio_format: { type: 'pcm16', sample_rate_hz: RATE }
      // no output_audio_format: we want TEXT and we’ll do TTS with ElevenLabs
    };
    upstream.send(JSON.stringify({ type: 'session.update', session }));
  });

  upstream.on('message', async (buf) => {
    if (!downstreamOpen) return;

    // forward for logging/visibility if you have a UI
    try { downstream.send(buf); } catch {}

    // voice-swap
    if (VOICE_SWAP !== 'elevenlabs' || !ELEVEN_API_KEY) return;

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
          // Default path: send to browser player (debug)
          try { broadcastSay(fullText); } catch {}

          // If you want Zoom output via Output Audio endpoint, uncomment below:
          // const botId = url.searchParams.get('bot_id'); // Recall may append this param
          // if (botId && RECALL_API_KEY) {
          //   const b64 = await elevenMp3Base64(fullText);
          //   await axios.post(`${RECALL_BASE}/bot/${encodeURIComponent(botId)}/output_audio/`,
          //     { kind: 'mp3', b64_data: b64 },
          //     { headers: { Authorization: RECALL_API_KEY, 'Content-Type': 'application/json' } }
          //   );
          // }
        }
      }
    } catch {
      /* ignore non-JSON frames */
    }
  });

  downstream.on('message', (msg) => { if (upstreamOpen) upstream.send(msg); });
  downstream.on('close', () => { downstreamOpen = false; try { upstream.close(); } catch {} });
  downstream.on('error', () => { downstreamOpen = false; try { upstream.close(); } catch {} });

  upstream.on('close', () => { if (downstreamOpen) try { downstream.close(); } catch {} });
  upstream.on('error', (e) => {
    console.error('[Realtime error]', e?.message);
    if (downstreamOpen) try { downstream.close(); } catch {}
  });
});

// ---------- Start ----------
server.listen(PORT, () => {
  console.log(`[Server] Listening on :${PORT}`);
  console.log(
    `VOICE_SWAP=${VOICE_SWAP} | PLAY_TO=${PLAY_TO} | RECALL_REGION=${RECALL_REGION}`
  );
});
