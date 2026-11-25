#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'case-pwa-disc-'));
process.env.CASE_HOME = scratch;
process.env.CASE_THREADS = path.join(scratch, 'threads.json');
const cased = http.createServer((req, res) => {
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ name: 'desk', credentials: [] }));
});
await new Promise((r) => cased.listen(0, '127.0.0.1', r));
process.env.CASE_URL = 'http://127.0.0.1:' + cased.address().port;
const { server } = await import('./serve.mjs');
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const sse = (type, data) => 'event: ' + type + '\ndata: ' + JSON.stringify({ type, ...data }) + '\n\n';
const answer = (text) => sse('message_start', { message: { id: 'm', type: 'message', role: 'assistant', model: 'm', content: [], stop_reason: null, usage: { input_tokens: 0, output_tokens: 1 } } })
  + sse('content_block_start', { index: 0, content_block: { type: 'text', text: '' } })
  + sse('content_block_delta', { index: 0, delta: { type: 'text_delta', text } })
  + sse('content_block_stop', { index: 0 })
  + sse('message_delta', { delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 5 } })
  + sse('message_stop', {});
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => resolve(new Response(answer('still finished after hangup'), { status: 200, headers: { 'content-type': 'text/event-stream' } })), 800);
  init.signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('aborted', 'AbortError')); });
});

function hangUp(p, body, waitBytes = 8) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, path: p, method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body), 'x-anthropic-key': 'sk-ant' },
    }, (res) => {
      let n = 0;
      res.on('data', (c) => {
        n += c.length;
        if (n >= waitBytes) { req.destroy(); resolve(true); }
      });
    });
    req.on('error', (err) => (err.code === 'ECONNRESET' ? resolve(true) : reject(err)));
    req.end(body);
  });
}

function call(p, { method = 'GET', body = '' } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, path: p, method,
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
    }, (res) => {
      let text = '';
      res.on('data', (c) => { text += c; });
      res.on('end', () => resolve({ status: res.statusCode || 0, json: text ? JSON.parse(text) : {} }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

async function waitFor(fn, ms = 5000) {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < ms) {
    last = await fn();
    if (last) return last;
    await new Promise((r) => setTimeout(r, 50));
  }
  return last;
}

const turn = JSON.stringify({ model: 'claude-sonnet-4-6', computer_id: 'c_1', input: 'keep going' });
const running = () => call('/api/threads').then((r) => (r.json.threads || []).find((t) => t.running) || null);

try {
  assert.equal(await hangUp('/api/chat', turn), true, 'chat stream hung up like a backgrounded PWA');
  const live = await waitFor(running);
  assert.ok(live, 'turn stays running after hangup');

  const unknown = await call('/api/chat/stop', { method: 'POST', body: JSON.stringify({ thread_id: 't_nope' }) });
  assert.equal(unknown.status, 404, 'stopping an unknown thread is refused');

  const finished = await waitFor(async () => {
    if (await running()) return null;
    const one = await call('/api/threads?id=' + encodeURIComponent(live.id));
    return (one.json.turns || []).some((x) => /still finished after hangup/.test(x.text || '')) ? one.json : null;
  });
  assert.ok(finished, 'turn completes after hangup');

  assert.equal(await hangUp('/api/chat', turn), true);
  assert.ok(await waitFor(running), 'second turn is running');
  const stop = await call('/api/chat/stop', { method: 'POST', body: '{}' });
  assert.equal(stop.status, 200);
  assert.equal(stop.json.stopped, true, 'an empty stop aborts the only running turn');
  assert.equal(await waitFor(async () => !(await running())), true, 'stop ends the turn');

  console.log('pwa disconnect: turn survives hangup, stop ends it');
} finally {
  globalThis.fetch = realFetch;
  server.close();
  cased.close();
  fs.rmSync(scratch, { recursive: true, force: true });
  process.exit(process.exitCode || 0);
}
