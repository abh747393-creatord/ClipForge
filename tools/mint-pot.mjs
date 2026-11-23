// Mint a YouTube WebPO (PO) token for ClipForge's yt-dlp downloads.
//
// YouTube sometimes answers automated requests with "Sign in to confirm
// you're not a bot". A PO token (proof of origin, minted through the same
// BotGuard challenge a real browser solves) makes yt-dlp's requests pass.
// The token is bound to a visitor_data value and lives ~12 hours — re-run
// this script when the bot check comes back.
//
// Flow:
//   1. Fetch the youtube.com homepage, extract VISITOR_DATA from ytcfg.
//   2. Ask BotGuard (jnn-pa.googleapis.com Waa/Create) for a challenge,
//      using the web client's request key.
//   3. Evaluate the returned BotGuard script in a JSDOM window, run the
//      challenge program, call GenerateIT for an integrity token.
//   4. Turn the integrity token into a WebPO token bound to visitor_data.
//   5. Write <clipforge>/data/pot.txt (po_token + visitor_data lines),
//      which app/media.py picks up automatically.
//
// The token itself is never printed. Only lengths are logged.

import { BG } from 'bgutils-js';
import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const TOOLS_DIR = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = process.env.CLIPFORGE_DATA_DIR
  ? path.resolve(process.env.CLIPFORGE_DATA_DIR)
  : path.join(TOOLS_DIR, '..', 'data');
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo'; // YouTube web-client BotGuard request key
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const HOMEPAGE = 'https://www.youtube.com/';

function extractVisitorData(html) {
  let m = html.match(/"VISITOR_DATA":"([^"]+)"/);
  if (m) return m[1];
  m = html.match(/"visitorData":"([^"]+)"/);
  if (m) return m[1];
  throw new Error('VISITOR_DATA not found in YouTube homepage');
}

async function main() {
  console.log('[mint] fetching YouTube homepage ...');
  const res = await fetch(HOMEPAGE, { headers: { 'User-Agent': USER_AGENT } });
  if (!res.ok) throw new Error(`Homepage fetch failed: HTTP ${res.status}`);
  const html = await res.text();
  const visitorData = extractVisitorData(html);
  console.log('[mint] visitorData length:', visitorData.length);

  // runScripts:'outside-only' is REQUIRED: without it, window.eval() runs in
  // Node's context where `window`/`document` are undefined and the BotGuard
  // script fails with "window is not defined".
  const dom = new JSDOM(html, { url: HOMEPAGE, runScripts: 'outside-only' });
  const { window } = dom;

  const bgConfig = {
    fetch,
    globalObj: window,
    identity: visitorData,
    requestKey: REQUEST_KEY,
  };

  console.log('[mint] requesting BotGuard challenge (Waa/Create) ...');
  const challenge = await BG.Challenge.create(bgConfig);
  if (!challenge) throw new Error('Challenge.create returned nothing');
  console.log('[mint] challenge ok — interpreterHash:', challenge.interpreterHash, '| globalName:', challenge.globalName);

  const script = (challenge.script || []).filter(Boolean).join('');
  if (!script) throw new Error('Challenge returned no script');
  window.eval(script);
  if (!window[challenge.globalName]) throw new Error(`BotGuard VM not found on window after eval (${challenge.globalName})`);
  console.log('[mint] BotGuard VM loaded, running program + GenerateIT ...');

  const poToken = await BG.PoToken.generate({
    program: challenge.challenge,
    bgConfig,
    globalName: challenge.globalName,
  });
  if (!poToken || poToken.length <= 80) throw new Error('PoToken.generate returned nothing usable');

  fs.mkdirSync(DATA_DIR, { recursive: true });
  const outFile = path.join(DATA_DIR, 'pot.txt');
  const mintedAt = new Date().toISOString();
  fs.writeFileSync(outFile,
    `minted_at=${mintedAt}\nvisitor_data=${visitorData}\npo_token=${poToken}\n`);
  console.log('[mint] OK — token length:', poToken.length, '| written to', outFile);
  console.log('[mint] ClipForge will use it automatically. It lasts ~12 hours.');
}

main().catch((e) => { console.error('[mint] FAILED:', e.message); process.exit(1); });
