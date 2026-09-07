#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Real-HTTP checks for the auth and Host/Origin gates. Run: node web/web-ui/test_http.mjs
import assert from 'node:assert/strict';
import http from 'node:http';

process.env.CASE_TOKEN = 'tok';
delete process.env.CASE_DRIVE_API_KEY;
delete process.env.CASE_DRIVE_PROVIDER;
const serve = await import('./serve.mjs');
const { server } = serve;
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const get = (p, headers = {}) => fetch(base + p, { headers, redirect: 'manual' });
const post = (p, body, headers = {}) => fetch(base + p, {
  method: 'POST', headers: { 'content-type': 'application/json', ...headers },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});

assert.equal((await get('/api/threads')).status, 401);
assert.equal((await get('/api/threads', { authorization: 'Bearer tok' })).status, 200);
assert.equal((await fetch(base + '/api/threads', { method: 'POST', headers: { authorization: 'Bearer tok', origin: 'https://evil.example' } })).status, 403);
assert.equal((await get('/?token=tok')).status, 302);
// fetch silently ignores a Host override; use http.get for the rebinding case.
const status = await new Promise((r) => http.get({ host: '127.0.0.1', port: server.address().port, path: '/api/threads',
  headers: { authorization: 'Bearer tok', host: 'evil.example' } }, (res) => { res.resume(); r(res.statusCode); }));
assert.equal(status, 403);

assert.equal((await get('/api/schedules')).status, 401);
assert.equal((await post('/api/brain', { computer_id: 'c_1', prompt: 'hi' })).status, 401);
assert.equal((await post('/api/brain', {}, { authorization: 'Bearer tok' })).status, 400);
assert.equal((await post('/api/brain', { computer_id: 'c_1', prompt: 'hi' }, { authorization: 'Bearer tok' })).status, 503);
{
  const h = await (await get('/api/health', { authorization: 'Bearer tok' })).json();
  assert.equal(h.brain_key, false);
}
const origTurn = serve.driveLoop.turn;
process.env.CASE_DRIVE_API_KEY = 'sk-test';
process.env.CASE_DRIVE_PROVIDER = 'openai';
let seen;
serve.driveLoop.turn = async (args) => { seen = args; return { text: 'did it', finished: true }; };
try {
  const r = await post('/api/brain', { computer_id: 'c_1', prompt: 'hi' }, { authorization: 'Bearer tok' });
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true, finished: true, text: 'did it' });
  assert.equal(seen.computerId, 'c_1');
  assert.equal(seen.inputText, 'hi');
  assert.equal(seen.thread.title, 'sched · hi');
  // cased's socket is the deadline: the turn gets an abort signal and a disconnect promise
  assert.ok(seen.signal instanceof AbortSignal);
  assert.ok(seen.disconnect instanceof Promise);
  const h = await (await get('/api/health', { authorization: 'Bearer tok' })).json();
  assert.equal(h.brain_key, true);

  await post('/api/brain', { computer_id: 'c_1', prompt: 'hi', name: 'morning sweep' }, { authorization: 'Bearer tok' });
  assert.equal(seen.thread.title, 'sched · morning sweep');

  serve.driveLoop.turn = async () => ({ error: 'provider down' });
  const bad = await post('/api/brain', { computer_id: 'c_1', prompt: 'hi' }, { authorization: 'Bearer tok' });
  assert.equal(bad.status, 200);
  assert.deepEqual(await bad.json(), { ok: false, error: 'provider down' });
} finally {
  serve.driveLoop.turn = origTurn;
  delete process.env.CASE_DRIVE_API_KEY;
  delete process.env.CASE_DRIVE_PROVIDER;
}

server.close();
console.log('test_http: ok');
