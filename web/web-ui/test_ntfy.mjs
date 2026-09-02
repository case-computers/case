#!/usr/bin/env node
// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import { envDriveAuth } from './case-tools.mjs';
import {
  OUTBOUND_TAG, authHeaders, clipNtfy, inboundText, isOutbound, listen,
  ntfyConfig, parseSseData, publish, tagsOf,
} from './ntfy.mjs';
import { startPhoneNtfy } from './serve.mjs';

assert.deepEqual(ntfyConfig({}), {
  url: 'https://ntfy.sh', topic: '', token: '', chat: false,
});
assert.deepEqual(ntfyConfig({
  CASE_NTFY_URL: 'https://ntfy.example/', CASE_NTFY_TOPIC: 'abc',
  CASE_NTFY_TOKEN: 'tok', CASE_NTFY_CHAT: '1',
}), { url: 'https://ntfy.example', topic: 'abc', token: 'tok', chat: true });
assert.equal(ntfyConfig({ CASE_NTFY_CHAT: 'true' }).chat, true);
assert.deepEqual(authHeaders('tok'), { Authorization: 'Bearer tok' });
assert.deepEqual(authHeaders(''), {});

assert.deepEqual(envDriveAuth({}), { provider: '', key: '' });
assert.deepEqual(envDriveAuth({ CASE_DRIVE_API_KEY: 'sk', CASE_DRIVE_PROVIDER: 'openai' }),
  { provider: 'openai', key: 'sk' });
assert.deepEqual(envDriveAuth({ CASE_DRIVE_API_KEY: 'sk', CASE_DRIVE_PROVIDER: 'anthropic' }),
  { provider: 'anthropic', key: 'sk' });
assert.deepEqual(envDriveAuth({ CASE_DRIVE_API_KEY: 'sk', CASE_DRIVE_PROVIDER: 'other' }),
  { provider: '', key: '' });

assert.deepEqual(tagsOf({ tags: ['case-outbound', 'h_1'] }), ['case-outbound', 'h_1']);
assert.deepEqual(tagsOf({ tags: 'case-outbound,h_1' }), ['case-outbound', 'h_1']);
assert.ok(isOutbound({ tags: [OUTBOUND_TAG] }));
assert.equal(inboundText({ event: 'message', message: 'hi', tags: [OUTBOUND_TAG] }), '');
assert.equal(inboundText({ event: 'keepalive', message: 'hi' }), '');
assert.equal(inboundText({ event: 'message', message: '  hi  ' }), 'hi');

{
  const { events, rest } = parseSseData(
    'data: {"id":"a","event":"message","message":"hi"}\n\n'
    + 'data: {"event":"keepalive"}\n\n'
    + 'data: {"id":"b","event":"message","message":"x"');
  assert.equal(events.length, 2);
  assert.equal(events[0].message, 'hi');
  assert.equal(events[1].event, 'keepalive');
  assert.match(rest, /"id":"b"/);
}

assert.ok(clipNtfy('x'.repeat(4000)).includes('open Drive for the rest'));
assert.equal(clipNtfy('short'), 'short');

{
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true };
  };
  await publish(
    { url: 'https://ntfy.sh', topic: 'top', token: 'secret' },
    { title: '[Case] done', message: 'ok' },
    fetchImpl,
  );
  assert.equal(calls[0].url, 'https://ntfy.sh/top');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(calls[0].opts.headers.Authorization, 'Bearer secret');
  assert.match(calls[0].opts.headers['X-Tags'], new RegExp(OUTBOUND_TAG));
  assert.equal(calls[0].opts.body, 'ok');
}

function sseBody(text) {
  return new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(text));
      c.close();
    },
  });
}

{
  const urls = [];
  const ac = new AbortController();
  const fetchImpl = async (url) => {
    urls.push(url);
    throw new Error('stop');
  };
  try {
    await listen(
      { url: 'https://ntfy.sh', topic: 'top', token: 'tok' },
      () => {},
      {
        fetchImpl, signal: ac.signal, now: () => 1700000000,
        sleep: async () => { ac.abort(); },
      },
    );
  } catch { /* aborted */ }
  assert.equal(urls[0], 'https://ntfy.sh/top/sse?since=1700000000');
}

{
  const got = [];
  const urls = [];
  const body = sseBody(
    'data: {"id":"1","event":"open"}\n\n'
    + 'data: {"id":"2","event":"message","message":"self","tags":["case-outbound"]}\n\n'
    + 'data: {"id":"3","event":"message","message":"check gmail","tags":[]}\n\n',
  );
  let n = 0;
  const ac = new AbortController();
  const fetchImpl = async (url, opts) => {
    urls.push(url);
    n += 1;
    assert.equal(opts.headers.Authorization, 'Bearer tok');
    if (n === 1) return { ok: true, body };
    ac.abort();
    throw new Error('stop');
  };
  try {
    await listen(
      { url: 'https://ntfy.sh', topic: 'top', token: 'tok' },
      (text) => { got.push(text); },
      { fetchImpl, signal: ac.signal, now: () => 1700000000, sleep: async () => {} },
    );
  } catch { /* aborted */ }
  assert.deepEqual(got, ['check gmail']);
  assert.match(urls[0], /since=1700000000/);
  assert.match(urls[1], /since=3/);
}

{
  const got = [];
  let n = 0;
  const ac = new AbortController();
  const fetchImpl = async () => {
    n += 1;
    if (n > 2) {
      ac.abort();
      throw new Error('stop');
    }
    return { ok: true, body: sseBody('data: {"id":"3","event":"message","message":"check gmail","tags":[]}\n\n') };
  };
  try {
    await listen(
      { url: 'https://ntfy.sh', topic: 'top' },
      (text) => { got.push(text); },
      { fetchImpl, signal: ac.signal, now: () => 1700000000, sleep: async () => {} },
    );
  } catch { /* aborted */ }
  assert.deepEqual(got, ['check gmail']);
}

assert.equal(startPhoneNtfy({}), false);
assert.equal(startPhoneNtfy({ CASE_NTFY_CHAT: '1' }), false);
assert.equal(startPhoneNtfy({ CASE_NTFY_CHAT: '1', CASE_NTFY_TOPIC: 't' }), false);

console.log('ok test_ntfy.mjs');
