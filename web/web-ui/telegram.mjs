// SPDX-License-Identifier: MIT
/**
 * Telegram transport for phone chat. The box owns the bot (CASE_TELEGRAM_TOKEN)
 * and long-polls getUpdates, so nothing listens on a public port. Exactly one
 * chat (CASE_TELEGRAM_CHAT_ID) may drive it; every other chat is ignored.
 */
import { routePhone } from './phone.mjs';

export const STALE_S = 600;
export const MAX_TEXT = 4000;
const CALLBACK_RE = /^h:(h_\w+):(approve|deny)$/;
const TOKEN_RE = /^\d+:[\w-]+$/;   // BotFather shape; anything else never reaches a URL

export function telegramConfig(env = process.env) {
  const raw = String(env.CASE_TELEGRAM_TOKEN || '').trim();
  return {
    token: TOKEN_RE.test(raw) ? raw : '',
    chatId: Number(env.CASE_TELEGRAM_CHAT_ID) || 0,
  };
}

export async function tgApi(token, method, body, fetchImpl = fetch, timeoutMs = 10000) {
  const r = await fetchImpl(`https://api.telegram.org/bot${token}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) {
    const err = new Error(`telegram ${method} ${r.status}: ${j.description || ''}`.trim());
    err.status = r.status;
    throw err;
  }
  return j.result;
}

/** One Bot API update → { chatId, msg }, or null for anything that is not
 *  text or a button tap in a private chat. */
export function parseUpdate(update) {
  const cq = update?.callback_query;
  if (cq?.message?.chat?.type === 'private' && typeof cq.data === 'string') {
    return {
      chatId: Number(cq.message.chat.id),
      msg: { kind: 'callback', data: cq.data.slice(0, 64), callbackId: String(cq.id) },
    };
  }
  const m = update?.message;
  if (!m || m.chat?.type !== 'private' || typeof m.text !== 'string') return null;
  return {
    chatId: Number(m.chat.id),
    msg: {
      kind: 'text',
      text: m.text.slice(0, MAX_TEXT).trim(),
      date: Number(m.date) || 0,
      replyTo: Number(m.reply_to_message?.message_id) || 0,
    },
  };
}

/** Telegram-only shapes first (buttons, reply-to-a-prompt, /start, stale),
 *  then the shared phone router. Replies to a code prompt carry their handoff
 *  id, so they skip the stale check and the one-pending guess. */
export function routeTelegram({
  msg, codePrompts = new Map(), pendingIds = [], busy = false, now = Math.floor(Date.now() / 1000),
}) {
  if (msg.kind === 'callback') {
    const m = CALLBACK_RE.exec(msg.data);
    return m ? { type: 'handoff', hid: m[1], value: m[2] } : { type: 'ignore' };
  }
  if (!msg.text) return { type: 'ignore' };
  if (msg.text === '/start') return { type: 'start' };
  if (msg.replyTo && codePrompts.has(msg.replyTo)) {
    return { type: 'handoff', hid: codePrompts.get(msg.replyTo), value: msg.text };
  }
  const age = now - msg.date;
  if (age > STALE_S) return { type: 'stale', text: msg.text, age };
  return routePhone({ text: msg.text, pendingIds, busy });
}

export function handoffMessage(h) {
  const who = h.domain || 'Your computer';
  const prompt = String(h.prompt || '').slice(0, 1500);
  if (h.kind === 'approval') {
    return {
      text: `${who} needs approval:\n${prompt}`,
      reply_markup: { inline_keyboard: [[
        { text: 'Approve', callback_data: `h:${h.id}:approve` },
        { text: 'Deny', callback_data: `h:${h.id}:deny` },
      ]] },
    };
  }
  return {
    text: `${who} needs you:\n${prompt}\n\nReply to this message with the code, or "done" once you have handled it.`,
    reply_markup: { force_reply: true, input_field_placeholder: 'Code' },
  };
}

export function chunk(text, n = 4000) {
  const s = String(text || '');
  const out = [];
  for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n));
  return out;
}

/** getUpdates loop. Updates are handed to onUpdate without awaiting it, so a
 *  running turn never blocks the next message (that is how steering works).
 *  Confirmed by the next offset; unconfirmed ones return after a restart and
 *  routeTelegram drops the stale ones. A 401 means the token is wrong: stop. */
export async function poll(cfg, onUpdate, {
  fetchImpl = fetch,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  signal,
} = {}) {
  let offset = 0;
  while (!signal?.aborted) {
    try {
      const updates = await tgApi(cfg.token, 'getUpdates', {
        offset, timeout: 30, allowed_updates: ['message', 'callback_query'],
      }, fetchImpl, 40000);
      for (const u of updates) {
        offset = u.update_id + 1;
        Promise.resolve().then(() => onUpdate(u)).catch((err) => {
          console.warn('telegram update:', err?.message || err);
        });
      }
      continue;
    } catch (err) {
      if (signal?.aborted) return;
      console.warn('telegram poll:', err?.message || err);
      if (err?.status === 401) return;
    }
    await sleep(5000);
  }
}
