#!/usr/bin/env node
/**
 * DingTalk source — connects to DingTalk Stream Mode SDK
 * and writes messages directly to msgbox central DB.
 *
 * Completely independent of the webhook server.
 *
 * Usage: node dingtalk.js <db_path> [client_id] [client_secret]
 *   or via env: DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET
 */

const path = require('path');
const sqlite3 = require('sqlite3').verbose();
const { DingTalkStream } = require('dingtalk-stream');

const dbPath = process.argv[2] || process.env.MSGBOX_DB_PATH;
const clientId = process.argv[3] || process.env.DINGTALK_CLIENT_ID;
const clientSecret = process.argv[4] || process.env.DINGTALK_CLIENT_SECRET;

if (!clientId || !clientSecret) {
  console.error('Usage: node dingtalk.js <db_path> <client_id> <client_secret>');
  console.error('Or set DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET, MSGBOX_DB_PATH');
  process.exit(1);
}

// ── DB wrapper (async) ──────────────────────────────────
class Db {
  constructor(dbPath) {
    this.db = new sqlite3.Database(dbPath);
  }
  run(sql, params = []) {
    return new Promise((resolve, reject) => {
      this.db.run(sql, params, function(err) {
        if (err) reject(err);
        else resolve(this.lastID);
      });
    });
  }
  exec(sql) {
    return new Promise((resolve, reject) => {
      this.db.exec(sql, (err) => err ? reject(err) : resolve());
    });
  }
  close() {
    this.db.close();
  }
}

async function initDb(db) {
  await db.exec(`CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'normal',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
  )`);
  // Migration for existing DBs
  try { await db.exec("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT ''"); } catch(e) {}
  try { await db.exec("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source)"); } catch(e) {}
}

async function insertMessage(db, type_, title, content, props, category) {
  return await db.run(
    "INSERT INTO messages (type, title, content, props, category, source) VALUES (?, ?, ?, ?, ?, ?)",
    [type_, title, content, JSON.stringify(props), category, 'dingtalk']
  );
}

// ── Main ─────────────────────────────────────────────────
async function main() {
  console.error(`[dingtalk] DB: ${dbPath}`);
  const db = new Db(dbPath);
  await initDb(db);

  const stream = new DingTalkStream({ clientId, clientSecret });

  stream.on('channel_message', async (data) => {
    const sender = data.senderNick || data.senderId || 'unknown';
    const conversation = data.conversationTitle || data.conversationId || 'chat';
    const text = (data.textContent || '').substring(0, 300);
    const hasAt = (data.textContent || '').includes('@');
    const category = hasAt ? 'popup' : 'normal';

    try {
      const id = await insertMessage(db, 'dingtalk.message',
        `DingTalk from ${sender} in ${conversation}`,
        text || '(non-text message)',
        {
          senderId: data.senderId || '',
          senderNick: sender,
          conversationId: data.conversationId || '',
          conversationTitle: conversation,
          conversationType: data.conversationType || '',
          msgType: data.msgType || '',
          source: 'dingtalk',
        },
        category
      );
      console.error(`[dingtalk] #${id} ${category}: ${sender} -> ${conversation}`);
    } catch (e) {
      console.error(`[dingtalk] db error:`, e.message);
    }
  });

  stream.on('callback', async (data) => {
    try {
      const id = await insertMessage(db, 'dingtalk.callback',
        `DingTalk callback: ${data.callbackType || 'unknown'}`,
        JSON.stringify(data).substring(0, 500),
        { callbackType: data.callbackType || '', source: 'dingtalk' },
        'popup'
      );
      console.error(`[dingtalk] #${id} callback: ${data.callbackType || ''}`);
    } catch (e) {
      console.error(`[dingtalk] db error:`, e.message);
    }
  });

  stream.on('connected', () => console.error(`[dingtalk] connected`));
  stream.on('disconnected', () => console.error(`[dingtalk] disconnected`));
  stream.on('error', (e) => console.error(`[dingtalk] error:`, e.message));

  await stream.connect();
  console.error(`[dingtalk] running`);

  process.on('SIGINT', async () => { db.close(); await stream.disconnect(); process.exit(0); });
  process.on('SIGTERM', async () => { db.close(); await stream.disconnect(); process.exit(0); });
}

main().catch(e => { console.error(`[dingtalk] fatal:`, e); process.exit(1); });
