#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Real-HTTP checks for the auth and Host/Origin gates. Run: node web/web-ui/test_http.mjs
import assert from 'node:assert/strict';
import http from 'node:http';

process.env.CASE_TOKEN = 'tok';
const { server } = await import('./serve.mjs');
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const get = (p, headers = {}) => fetch(base + p, { headers, redirect: 'manual' });

assert.equal((await get('/api/threads')).status, 401);
assert.equal((await get('/api/threads', { authorization: 'Bearer tok' })).status, 200);
assert.equal((await fetch(base + '/api/threads', { method: 'POST', headers: { authorization: 'Bearer tok', origin: 'https://evil.example' } })).status, 403);
assert.equal((await get('/?token=tok')).status, 302);
// fetch silently ignores a Host override; use http.get for the rebinding case.
const status = await new Promise((r) => http.get({ host: '127.0.0.1', port: server.address().port, path: '/api/threads',
  headers: { authorization: 'Bearer tok', host: 'evil.example' } }, (res) => { res.resume(); r(res.statusCode); }));
assert.equal(status, 403);

server.close();
console.log('test_http: ok');
