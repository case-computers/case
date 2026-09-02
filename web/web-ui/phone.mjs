// SPDX-License-Identifier: MIT
/**
 * Phone chat routing shared by every transport (ntfy, Telegram). A transport
 * turns its wire format into text; this decides what the text means.
 */
export const PHONE_THREAD_ID = 't_phone';
const HANDOFF_RE = /^(h_\w+)\s+(.+)$/s;
const RESERVED_RE = /^(approve|deny|done|i'm done|im done|i am done|\d+)$/i;

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
