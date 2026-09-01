// SPDX-License-Identifier: MIT
export const OUTBOUND_TAG = 'case-outbound';
export const PHONE_THREAD_ID = 't_phone';
const HANDOFF_RE = /^(h_\w+)\s+(.+)$/s;
const RESERVED_RE = /^(approve|deny|done|i'm done|im done|i am done|\d+)$/i;

export function ntfyConfig(env = process.env) {
  const chat = ['1', 'true'].includes(String(env.CASE_NTFY_CHAT || '').trim().toLowerCase());
  return {
    url: String(env.CASE_NTFY_URL || 'https://ntfy.sh').replace(/\/+$/, ''),
    topic: String(env.CASE_NTFY_TOPIC || '').trim(),
    token: String(env.CASE_NTFY_TOKEN || '').trim(),
    chat,
  };
}

export function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function tagsOf(ev) {
  const t = ev?.tags;
  if (Array.isArray(t)) return t.map(String);
  if (typeof t === 'string') return t.split(',').map((s) => s.trim()).filter(Boolean);
  return [];
}

export function isOutbound(ev) {
  return tagsOf(ev).includes(OUTBOUND_TAG);
}

export function inboundText(ev) {
  if (!ev || ev.event !== 'message' || isOutbound(ev)) return '';
  return String(ev.message || '').trim();
}

export function parseSseData(chunk, carry = '') {
  const text = carry + chunk;
  const parts = text.split('\n\n');
  const rest = parts.pop() ?? '';
  const events = [];
  for (const block of parts) {
    const data = block.split('\n')
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).trimStart())
      .join('\n');
    if (!data) continue;
    try { events.push(JSON.parse(data)); } catch { /* keep-alive / malformed */ }
  }
  return { events, rest };
}

export function parseHandoffReply(text) {
  const m = HANDOFF_RE.exec(String(text || '').trim());
  if (m) return { hid: m[1], value: m[2].trim() };
  return { hid: null, value: String(text || '').trim() };
}

export function routePhone({ text, pendingIds = [], busy = false }) {
  const raw = String(text || '').trim();
  if (!raw) return { type: 'ignore' };
  const parsed = parseHandoffReply(raw);
  if (parsed.hid) {
    if (!pendingIds.includes(parsed.hid)) {
      return { type: 'error', error: `no pending handoff ${parsed.hid}` };
    }
    return { type: 'handoff', hid: parsed.hid, value: parsed.value };
  }
  if (pendingIds.length === 1) {
    return { type: 'handoff', hid: pendingIds[0], value: parsed.value };
  }
  if (pendingIds.length > 1) {
    return { type: 'error', error: `${pendingIds.length} pending handoffs; prefix with handoff id` };
  }
  if (RESERVED_RE.test(parsed.value)) return { type: 'error', error: 'Nothing waiting.' };
  if (busy) return { type: 'steer', text: parsed.value };
  return { type: 'task', text: parsed.value };
}

export function clipNtfy(s, n = 3500) {
  const t = String(s || '');
  return t.length <= n ? t : `${t.slice(0, n)}\n…open Drive for the rest`;
}

function asciiHeader(s) {
  return String(s || '').replace(/[^\x20-\x7e]/g, '?');
}

export async function publish(cfg, { title, message, tags = [] }, fetchImpl = fetch) {
  if (!cfg?.topic) return;
  const headers = {
    ...authHeaders(cfg.token),
    'X-Title': asciiHeader(title).slice(0, 200),
    'X-Tags': [OUTBOUND_TAG, ...tags].join(','),
    'Content-Type': 'text/plain; charset=utf-8',
  };
  const r = await fetchImpl(`${cfg.url}/${encodeURIComponent(cfg.topic)}`, {
    method: 'POST',
    headers,
    body: clipNtfy(message),
  });
  if (!r.ok) throw new Error(`ntfy publish ${r.status}`);
}

export async function listen(cfg, onMessage, {
  fetchImpl = fetch,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  signal,
  now = () => Math.floor(Date.now() / 1000),
} = {}) {
  if (!cfg?.topic) return;
  let since = String(now());
  const seen = new Set();
  const dec = new TextDecoder();
  while (!signal?.aborted) {
    try {
      const r = await fetchImpl(`${cfg.url}/${encodeURIComponent(cfg.topic)}/sse?since=${encodeURIComponent(since)}`, {
        headers: { ...authHeaders(cfg.token), Accept: 'text/event-stream' },
        signal,
      });
      if (!r.ok || !r.body) throw new Error(`ntfy subscribe ${r.status}`);
      let carry = '';
      for await (const chunk of r.body) {
        const { events, rest } = parseSseData(dec.decode(chunk, { stream: true }), carry);
        carry = rest;
        for (const ev of events) {
          if (ev.id) since = ev.id;
          const text = inboundText(ev);
          if (!text) continue;
          if (ev.id && seen.has(ev.id)) continue;
          if (ev.id) {
            seen.add(ev.id);
            if (seen.size > 200) seen.delete(seen.values().next().value);
          }
          await onMessage(text, ev);
        }
      }
    } catch (err) {
      if (signal?.aborted) return;
      console.warn('ntfy listen:', err?.message || err);
    }
    if (!signal?.aborted) await sleep(5000);
  }
}
