#!/usr/bin/env node
// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import {
  MAX_TEXT, STALE_S, chunk, handoffMessage, parseUpdate, poll, routeTelegram, telegramConfig, tgApi,
} from './telegram.mjs';

assert.deepEqual(telegramConfig({}), { token: '', chatId: 0 });
assert.deepEqual(telegramConfig({ CASE_TELEGRAM_TOKEN: ' tok ', CASE_TELEGRAM_CHAT_ID: '42' }),
  { token: 'tok', chatId: 42 });
assert.equal(telegramConfig({ CASE_TELEGRAM_CHAT_ID: 'abc' }).chatId, 0);

{
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    return { status: 200, json: async () => ({ ok: true, result: { message_id: 7 } }) };
  };
  const r = await tgApi('tok', 'sendMessage', { chat_id: 1, text: 'hi' }, fetchImpl);
  assert.deepEqual(r, { message_id: 7 });
  assert.equal(calls[0].url, 'https://api.telegram.org/bottok/sendMessage');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).text, 'hi');
}
{
  const fetchImpl = async () => ({ status: 409, json: async () => ({ ok: false, description: 'Conflict: webhook active' }) });
  await assert.rejects(() => tgApi('tok', 'getUpdates', {}, fetchImpl), (err) => {
    assert.equal(err.status, 409);
    assert.match(err.message, /getUpdates 409: Conflict: webhook active/);
    assert.doesNotMatch(err.message, /tok/);
    return true;
  });
}

const text = (t, extra = {}) => ({
  message: { message_id: 5, date: 1000, text: t, chat: { id: 42, type: 'private' }, ...extra },
});
assert.deepEqual(parseUpdate(text('check mail')), {
  chatId: 42, msg: { kind: 'text', text: 'check mail', date: 1000, replyTo: 0 },
});
assert.deepEqual(parseUpdate(text('  483920 ', { reply_to_message: { message_id: 3 } })).msg,
  { kind: 'text', text: '483920', date: 1000, replyTo: 3 });
assert.equal(parseUpdate(text('x'.repeat(MAX_TEXT + 5))).msg.text.length, MAX_TEXT);
assert.equal(parseUpdate({ message: { message_id: 1, date: 1, text: 'x', chat: { id: 1, type: 'group' } } }), null);
assert.equal(parseUpdate({ message: { message_id: 1, date: 1, chat: { id: 1, type: 'private' }, photo: [] } }), null);
assert.equal(parseUpdate({ edited_message: {} }), null);
assert.deepEqual(parseUpdate({
  callback_query: { id: 'cb1', data: 'h:h_ab12:approve', message: { message_id: 9, chat: { id: 42, type: 'private' } } },
}), { chatId: 42, msg: { kind: 'callback', data: 'h:h_ab12:approve', callbackId: 'cb1' } });

const now = 5000;
const msg = (t, extra = {}) => ({ kind: 'text', text: t, date: now - 5, replyTo: 0, ...extra });
assert.deepEqual(routeTelegram({ msg: msg(''), now }), { type: 'ignore' });
assert.deepEqual(routeTelegram({ msg: msg('/start'), now }), { type: 'start' });
assert.deepEqual(routeTelegram({ msg: msg('check mail'), now }), { type: 'task', text: 'check mail' });
assert.deepEqual(routeTelegram({ msg: msg('faster'), busy: true, now }), { type: 'steer', text: 'faster' });
assert.deepEqual(routeTelegram({ msg: msg('old', { date: now - STALE_S - 1 }), now }),
  { type: 'stale', text: 'old', age: STALE_S + 1 });
assert.deepEqual(routeTelegram({ msg: msg('483920'), pendingIds: ['h_9'], now }),
  { type: 'handoff', hid: 'h_9', value: '483920' });
// A reply to a code prompt names its handoff, even when stale or ambiguous.
assert.deepEqual(routeTelegram({
  msg: msg('483920', { replyTo: 11, date: now - 900 }), codePrompts: new Map([[11, 'h_ab12']]),
  pendingIds: ['h_ab12', 'h_zz'], now,
}), { type: 'handoff', hid: 'h_ab12', value: '483920' });
assert.deepEqual(routeTelegram({ msg: { kind: 'callback', data: 'h:h_ab12:deny', callbackId: 'c1' }, now }),
  { type: 'handoff', hid: 'h_ab12', value: 'deny' });
assert.deepEqual(routeTelegram({ msg: { kind: 'callback', data: 'junk', callbackId: 'c2' }, now }), { type: 'ignore' });

assert.deepEqual(handoffMessage({ id: 'h_1', kind: 'approval', prompt: 'Buy 3 licences?', domain: 'coupa.com' }), {
  text: 'coupa.com needs approval:\nBuy 3 licences?',
  reply_markup: { inline_keyboard: [[
    { text: 'Approve', callback_data: 'h:h_1:approve' },
    { text: 'Deny', callback_data: 'h:h_1:deny' },
  ]] },
});
assert.deepEqual(handoffMessage({ id: 'h_2', kind: 'question', prompt: 'Enter the 6-digit code', domain: null }), {
  text: 'Your computer needs you:\nEnter the 6-digit code\n\nReply to this message with the code, or "done" once you have handled it.',
  reply_markup: { force_reply: true, input_field_placeholder: 'Code' },
});
assert.equal(handoffMessage({ id: 'h_3', kind: 'approval', prompt: 'p'.repeat(2000) }).text.length, 'Your computer needs approval:\n'.length + 1500);

assert.deepEqual(chunk('a'.repeat(8100)).map((s) => s.length), [4000, 4000, 100]);
assert.deepEqual(chunk(''), []);
assert.deepEqual(chunk('x', 1), ['x']);

{
  // One batch, then a 409 that stops the loop through sleep().
  const bodies = [];
  const got = [];
  const ac = new AbortController();
  let n = 0;
  const fetchImpl = async (url, opts) => {
    bodies.push(JSON.parse(opts.body));
    n += 1;
    if (n === 1) return { status: 200, json: async () => ({ ok: true, result: [{ update_id: 10, a: 1 }, { update_id: 11, a: 2 }] }) };
    return { status: 409, json: async () => ({ ok: false, description: 'Conflict' }) };
  };
  await poll({ token: 'tok' }, (u) => { got.push(u.update_id); }, {
    fetchImpl, signal: ac.signal, sleep: async () => { ac.abort(); },
  });
  assert.deepEqual(got, [10, 11]);
  assert.equal(bodies[0].offset, 0);
  assert.equal(bodies[0].timeout, 30);
  assert.deepEqual(bodies[0].allowed_updates, ['message', 'callback_query']);
  assert.equal(bodies[1].offset, 12);
}
{
  // A bad token ends the loop instead of retrying forever.
  let n = 0;
  const fetchImpl = async () => { n += 1; return { status: 401, json: async () => ({ ok: false, description: 'Unauthorized' }) }; };
  await poll({ token: 'tok' }, () => {}, { fetchImpl, sleep: async () => { throw new Error('should not sleep'); } });
  assert.equal(n, 1);
}
{
  // Handler failures are logged, not fatal, and do not block the next update.
  const got = [];
  const ac = new AbortController();
  let n = 0;
  const fetchImpl = async () => {
    n += 1;
    if (n === 1) return { status: 200, json: async () => ({ ok: true, result: [{ update_id: 1 }, { update_id: 2 }] }) };
    ac.abort();
    return { status: 200, json: async () => ({ ok: true, result: [] }) };
  };
  await poll({ token: 'tok' }, async (u) => { got.push(u.update_id); if (u.update_id === 1) throw new Error('boom'); },
    { fetchImpl, signal: ac.signal, sleep: async () => {} });
  assert.deepEqual(got, [1, 2]);
}

console.log('ok test_telegram.mjs');
