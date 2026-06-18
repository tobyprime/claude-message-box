#!/usr/bin/env node
/**
 * DingTalk source — connects to DingTalk Stream Mode SDK
 * and forwards events to the local msgbox webhook endpoint.
 *
 * Architecture: DingTalk → Stream SDK → POST /webhook → msgbox DB
 *
 * Usage: node dingtalk.js <client_id> <client_secret>
 */

const { DingTalkStream } = require('dingtalk-stream');

const clientId = process.argv[2] || process.env.DINGTALK_CLIENT_ID;
const clientSecret = process.argv[3] || process.env.DINGTALK_CLIENT_SECRET;
const targetUrl = process.env.MSGBOX_WEBHOOK_URL || 'http://127.0.0.1:3001/webhook';

if (!clientId || !clientSecret) {
  console.error('Usage: node dingtalk.js <client_id> <client_secret>');
  console.error('Or set DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET');
  process.exit(1);
}

const http = require('http');

function postToWebhook(eventType, payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const url = new URL(targetUrl);
    const req = http.request(targetUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'X-DingTalk-Event': eventType,
        'X-DingTalk-Delivery': Date.now().toString(),
      },
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function main() {
  console.error(`[dingtalk] Target: ${targetUrl}`);

  const stream = new DingTalkStream({ clientId, clientSecret });

  stream.on('callback', async (data) => {
    console.error(`[dingtalk] callback:`, data.callbackType || '');
    try {
      const r = await postToWebhook('callback', data);
      console.error(`[dingtalk] forwarded callback -> ${r.status}`);
    } catch (e) { console.error(`[dingtalk] forward error:`, e.message); }
  });

  stream.on('channel_message', async (data) => {
    const sender = data.senderNick || data.senderId || '?';
    const text = (data.textContent || '').substring(0, 100);
    console.error(`[dingtalk] msg from ${sender}: ${text}`);
    try {
      const r = await postToWebhook('channel_message', data);
      console.error(`[dingtalk] forwarded msg -> ${r.status}`);
    } catch (e) { console.error(`[dingtalk] forward error:`, e.message); }
  });

  stream.on('error', (e) => console.error(`[dingtalk] error:`, e.message));
  stream.on('connected', () => console.error(`[dingtalk] connected`));
  stream.on('disconnected', () => console.error(`[dingtalk] disconnected`));

  await stream.connect();
  console.error(`[dingtalk] running`);

  process.on('SIGINT', async () => { await stream.disconnect(); process.exit(0); });
  process.on('SIGTERM', async () => { await stream.disconnect(); process.exit(0); });
}

main().catch(e => { console.error(`[dingtalk] fatal:`, e); process.exit(1); });
