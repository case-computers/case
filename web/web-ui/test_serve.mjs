#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Pure-unit checks for the drive v2 server helpers. Run: node web/web-ui/test_serve.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { shq, pathOk, parseFind, mimeFor, histTrim, histCloseOpenCalls, normHost, threadTurns, parseCaseUrl, liveCid, liveDestPath, livePathHasDotDot, tokenMatches, liveTarget, extraPlan, isLocalMode, pageFile } from './serve.mjs';
import {
  CASE_TOOLS, chatAuth, resolveChatModel, openaiToolsToAnthropic,
  newAnthropicStreamCtx, anthropicEventToNdjson, tracesFromAnthropicMessage,
  histToAnthropicMessages, anthropicThinkingFor,
} from './case-tools.mjs';

const html = fs.readFileSync(fileURLToPath(new URL('./index.html', import.meta.url)), 'utf8');
assert.match(html, /x-anthropic-key/);
assert.match(html, /ANTHROPIC KEY/);
assert.match(html, /claude-sonnet-4-6/);

// threadTurns: reopening a thread shows text + tool calls; outputs and reasoning stay server-side
const view = threadTurns([
  { role: 'user', content: 'do the thing' },
  { type: 'reasoning', summary: [] },
  { type: 'function_call', call_id: 'c1', name: 'computer_snapshot', arguments: '{"x":1}' },
  { type: 'function_call_output', call_id: 'c1', output: 'SECRET-ish page dump' },
  { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'done' }] },
]);
assert.deepEqual(view.map((t) => t.who), ['you', 'tool', 'agent']);
assert.equal(view[1].name, 'computer_snapshot');
assert.ok(!JSON.stringify(view).includes('SECRET'));   // outputs never reach the reopen view

// normHost: bare lowercase host or nothing — creds domains must never carry paths/creds
assert.equal(normHost('https://Mail.Google.com/mail/u/0'), 'mail.google.com');
assert.equal(normHost('x.com'), 'x.com');
assert.equal(normHost('user:pw@x.com:443'), 'x.com');
assert.equal(normHost('not a host'), '');
assert.equal(normHost('localhost'), '');   // needs a dot: bare words are typos, not sites
assert.equal(normHost(''), '');

// shq: single-quote safe for the exec shell
assert.equal(shq('/home/agent'), "'/home/agent'");
assert.equal(shq("a'b"), "'a'\\''b'");
assert.equal(shq('$(rm -rf /)'), "'$(rm -rf /)'");

// pathOk: absolute, no traversal, no NUL
assert.ok(pathOk('/home/agent'));
assert.ok(pathOk('/'));
assert.ok(pathOk('/home/agent/my file.txt'));
assert.ok(!pathOk('home/agent'));
assert.ok(!pathOk('/home/../etc/shadow'));
assert.ok(!pathOk('/..'));
assert.ok(!pathOk(''));
assert.ok(!pathOk('/a\0b'));
assert.ok(pathOk('/home/agent/..hidden')); // dots in names are fine, '..' segments are not

// parseFind: real find -printf output shape (tabs in names survive)
const out = parseFind(
  'f\t3526\t1778185982.0000000000\t.bashrc\n' +
  'd\t4096\t1786480978.5580782430\tDesktop\n' +
  'l\t11\t1786480978.0\tlink\n' +
  'f\t10\t1786480978.0\tweird\tname\n' +
  '\n');
assert.equal(out.length, 4);
assert.deepEqual(out[0], { name: 'Desktop', dir: true, link: false, size: 4096, mtime: 1786480978 });
assert.ok(out.find((e) => e.name === 'weird\tname'));
assert.ok(!out[1].dir && out.some((e) => e.link));
// dirs first, then names alphabetical
assert.deepEqual(out.map((e) => e.name), ['Desktop', '.bashrc', 'link', 'weird\tname']);

// mimeFor: previewable types + safe default
assert.equal(mimeFor('/a/b.png'), 'image/png');
assert.equal(mimeFor('/a/b.md'), 'text/plain; charset=utf-8');
assert.equal(mimeFor('/a/b.PY'.toLowerCase()), 'text/plain; charset=utf-8');
assert.equal(mimeFor('/a/b'), 'application/octet-stream');
assert.equal(mimeFor('/a/b.exe'), 'application/octet-stream');

// histTrim: conversation memory drops WHOLE turns, never splitting a function_call
// from its output (an orphan of either kind 400s every later request).
const turn = (n, pad) => [
  { role: 'user', content: `ask ${n}${pad}` },
  { type: 'function_call', call_id: `c${n}`, name: 'computer_snapshot', arguments: '{}' },
  { type: 'function_call_output', call_id: `c${n}`, output: 'ok' },
];
const h = { items: [...turn(1, 'x'.repeat(400)), ...turn(2, ''), ...turn(3, '')] };
histTrim(h, 500);   // 938 chars over three turns; dropping the fat first turn leaves 359
assert.deepEqual(h.items.map((i) => i.call_id || i.content), ['ask 2', 'c2', 'c2', 'ask 3', 'c3', 'c3']);
for (const it of h.items.filter((i) => i.type === 'function_call')) {
  assert.ok(h.items.some((o) => o.type === 'function_call_output' && o.call_id === it.call_id),
    `call ${it.call_id} lost its output`);
}
// the newest turn is never trimmed away, however far over budget it is
const solo = { items: turn(9, 'y'.repeat(5000)) };
histTrim(solo, 10);
assert.equal(solo.items.length, 3);
// under budget: untouched
const small = { items: turn(1, '') };
histTrim(small, 100000);
assert.equal(small.items.length, 3);

const closed = histCloseOpenCalls([
  { type: 'reasoning', summary: [] },
  { type: 'function_call', call_id: 'call_orphan', name: 'auth_attempt_wait', arguments: '{}' },
  { type: 'function_call', call_id: 'call_ok', name: 'computer_eval', arguments: '{}' },
  { type: 'function_call_output', call_id: 'call_ok', output: 'ok' },
]);
assert.ok(!closed.some((it) => it.type === 'reasoning'));
assert.equal(closed.filter((it) => it.type === 'function_call_output' && it.call_id === 'call_orphan').length, 1);
assert.equal(closed.filter((it) => it.call_id === 'call_ok').length, 2);
const kept = histCloseOpenCalls([{ type: 'reasoning', summary: [] }], { keepReasoning: true });
assert.equal(kept[0].type, 'reasoning');

assert.deepEqual(parseCaseUrl('http://cased:8787'), { hostname: 'cased', port: 8787, protocol: 'http:' });
assert.equal(isLocalMode({ CASE_LOCAL: '1' }, 'example.com'), true);
assert.equal(isLocalMode({ CASE_LOCAL: '0' }, '127.0.0.1'), false);
assert.equal(isLocalMode({}, '127.0.0.1'), true);
assert.equal(isLocalMode({}, 'cased'), true);
assert.equal(isLocalMode({}, 'remote.example'), false);
assert.equal(liveCid('/live/c_abc12/vnc.html'), 'c_abc12');
assert.equal(liveCid('/live/vnc.html'), '');
assert.equal(liveDestPath('/live/c_abc12/vnc.html?autoconnect=1'), '/vnc.html?autoconnect=1');
assert.equal(liveDestPath('/live/c_abc12/websockify'), '/websockify');
assert.ok(livePathHasDotDot('/live/c_abc12/foo/../../etc/passwd'));
assert.ok(livePathHasDotDot('/live/c_abc12/../vnc.html'));
assert.ok(livePathHasDotDot('/live/c_abc12/%2e%2e/vnc.html'));
assert.ok(!livePathHasDotDot('/live/c_abc12/vnc.html?autoconnect=1'));
assert.ok(!livePathHasDotDot('/live/c_abc12/websockify'));
assert.ok(tokenMatches({ headers: {}, url: '/' }, ''));
assert.ok(!tokenMatches({ headers: {}, url: '/' }, 'secret'));
assert.ok(tokenMatches({ headers: { authorization: 'Bearer secret' }, url: '/' }, 'secret'));
assert.ok(tokenMatches({ headers: { cookie: 'case_token=secret' }, url: '/' }, 'secret'));
assert.ok(tokenMatches({ headers: {}, url: '/?token=secret' }, 'secret'));

process.env.CASE_DOCKER_NETWORK = 'case';
assert.deepEqual(liveTarget('c_abc12'), { hostname: 'case-c_abc12', port: 6080 });
delete process.env.CASE_DOCKER_NETWORK;
assert.equal(liveTarget('c_abc12'), null);
assert.equal(liveTarget(''), null);

const loginPlan = extraPlan('computer_login', { credential: 'x.com', url: 'https://x.com' }, 'c_ab');
assert.equal(loginPlan.method, 'POST');
assert.equal(loginPlan.rel, '/computers/c_ab/login?wake=true');
assert.deepEqual(loginPlan.body, { credential: 'x.com', url: 'https://x.com' });
assert.ok(loginPlan.timeoutMs >= 180000, 'login must outlive deskd inject');
const waitPlan = extraPlan('auth_attempt_wait', { attempt_id: 'a_1', since_revision: 2, max_wait_s: 60 }, 'c_ab');
assert.equal(waitPlan.method, 'GET');
assert.match(waitPlan.rel, /^\/auth-attempts\/a_1\/wait\?/);
assert.match(waitPlan.rel, /after_revision=2/);
assert.match(waitPlan.rel, /timeout_s=60/);

const shot = extraPlan('computer_screenshot', {}, 'c_ab');
assert.equal(shot.method, 'GET');
assert.equal(shot.rel, '/computers/c_ab/screenshot?wake=true');
assert.ok(shot.screenshot);
const capStart = extraPlan('computer_capture_start', { url_pattern: 'graphql' }, 'c_ab');
assert.equal(capStart.method, 'POST');
assert.equal(capStart.rel, '/computers/c_ab/capture?wake=true');
assert.deepEqual(capStart.body, { pattern: 'graphql' });
const capRead = extraPlan('computer_capture_read', {}, 'c_ab');
assert.equal(capRead.method, 'GET');
const capStop = extraPlan('computer_capture_read', { stop: true }, 'c_ab');
assert.equal(capStop.method, 'DELETE');
const put = extraPlan('computer_file_put', { path: '/home/agent/a.txt', content_b64: 'YQ==' }, 'c_ab');
assert.equal(put.method, 'PUT');
assert.ok(put.rel.includes('/files?path='));
assert.ok(put.rawPut);
const slp = extraPlan('computer_sleep', {}, 'c_ab');
assert.equal(slp.method, 'POST');
assert.equal(slp.rel, '/computers/c_ab/sleep');
const hr = extraPlan('handoff_request', { prompt: 'ok?', kind: 'approval' }, 'c_ab');
assert.equal(hr.rel, '/computers/c_ab/handoffs');
assert.deepEqual(hr.body, { kind: 'approval', prompt: 'ok?' });
assert.equal(extraPlan('handoff_list', {}, 'c_ab').rel, '/handoffs?status=pending');
assert.equal(extraPlan('handoff_get', { handoff_id: 'h_1' }, 'c_ab').rel, '/handoffs/h_1');
assert.equal(extraPlan('computer_list', {}, 'c_ab').rel, '/computers');
assert.equal(extraPlan('computer_create', { name: 'desk' }, 'c_ab').method, 'POST');
assert.deepEqual(extraPlan('computer_create', { name: 'desk' }, 'c_ab').body, { name: 'desk' });

assert.deepEqual(chatAuth({ 'x-openai-key': 'sk-openai' }), { provider: 'openai', key: 'sk-openai' });
assert.deepEqual(chatAuth({ 'x-anthropic-key': 'sk-ant-test' }), { provider: 'anthropic', key: 'sk-ant-test' });
assert.deepEqual(chatAuth({}), { provider: '', key: '' });
assert.equal(chatAuth({ 'x-anthropic-key': 'sk-ant-test', 'x-openai-key': 'sk-openai' }).provider, 'anthropic');
assert.equal(resolveChatModel('gpt-5.6-luna', 'openai'), 'gpt-5.6-luna');
assert.equal(resolveChatModel('nope', 'openai'), 'gpt-5.6-terra');
assert.equal(resolveChatModel('claude-opus-4-6', 'anthropic'), 'claude-opus-4-6');
assert.equal(resolveChatModel('gpt-5.6-terra', 'anthropic'), 'claude-sonnet-4-6');
{
  const tools = openaiToolsToAnthropic(CASE_TOOLS);
  assert.equal(tools[0].name, 'computer_navigate');
  assert.equal(tools[0].input_schema.required[0], 'url');
  assert.equal(tools[0].type, undefined);
}
{
  const ctx = newAnthropicStreamCtx();
  const think = anthropicEventToNdjson({
    type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'look ' },
  }, ctx);
  assert.deepEqual(think, { type: 'think_delta', text: 'look ' });
  const start = anthropicEventToNdjson({
    type: 'content_block_start', index: 1,
    content_block: { type: 'tool_use', id: 'tu_1', name: 'computer_eval', input: {} },
  }, ctx);
  assert.equal(start.type, 'tool');
  assert.equal(start.name, 'computer_eval');
  assert.equal(start.call_id, 'tu_1');
  const d1 = anthropicEventToNdjson({
    type: 'content_block_delta', index: 1, delta: { type: 'input_json_delta', partial_json: '{"expression"' },
  }, ctx);
  assert.equal(d1.type, 'tool_args_delta');
  anthropicEventToNdjson({
    type: 'content_block_delta', index: 1, delta: { type: 'input_json_delta', partial_json: ':"1"}' },
  }, ctx);
  const done = anthropicEventToNdjson({ type: 'content_block_stop', index: 1 }, ctx);
  assert.deepEqual(done.args, { expression: '1' });
  const tx = anthropicEventToNdjson({
    type: 'content_block_delta', index: 2, delta: { type: 'text_delta', text: 'ok' },
  }, ctx);
  assert.deepEqual(tx, { type: 'text_delta', text: 'ok' });
  assert.equal(anthropicEventToNdjson({ type: 'message_stop' }, ctx), null);
}
{
  const t = tracesFromAnthropicMessage({
    content: [
      { type: 'thinking', thinking: 'open hn' },
      { type: 'tool_use', id: 'tu_1', name: 'computer_navigate', input: { url: 'https://news.ycombinator.com' } },
      { type: 'text', text: 'done' },
    ],
  });
  assert.deepEqual(t.thinks, ['open hn']);
  assert.equal(t.calls[0].name, 'computer_navigate');
  assert.equal(t.calls[0].call_id, 'tu_1');
  assert.deepEqual(JSON.parse(t.calls[0].arguments), { url: 'https://news.ycombinator.com' });
  assert.deepEqual(t.texts, ['done']);
}
{
  const msgs = histToAnthropicMessages([
    { role: 'developer', content: 'sys' },
    { role: 'user', content: 'do the thing' },
    { type: 'function_call', call_id: 'c1', name: 'computer_snapshot', arguments: '{"x":1}' },
    { type: 'function_call_output', call_id: 'c1', output: 'page dump' },
    { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'done' }] },
  ]);
  assert.equal(msgs[0].role, 'user');
  assert.equal(msgs[0].content, 'do the thing');
  assert.equal(msgs[1].role, 'assistant');
  assert.equal(msgs[1].content[0].type, 'tool_use');
  assert.equal(msgs[1].content[0].id, 'c1');
  assert.deepEqual(msgs[1].content[0].input, { x: 1 });
  assert.equal(msgs[2].role, 'user');
  assert.equal(msgs[2].content[0].type, 'tool_result');
  assert.equal(msgs[2].content[0].tool_use_id, 'c1');
  assert.equal(msgs[3].role, 'assistant');
  assert.equal(msgs[3].content[0].text, 'done');
  assert.ok(!JSON.stringify(msgs).includes('developer'));
}
assert.deepEqual(anthropicThinkingFor([{ role: 'user', content: 'hi' }]), { type: 'adaptive' });
assert.deepEqual(anthropicThinkingFor([
  { role: 'user', content: 'hi' },
  { role: 'assistant', content: [{ type: 'tool_use', id: '1', name: 'computer_eval', input: {} }] },
]), { type: 'disabled' });
assert.deepEqual(anthropicThinkingFor([
  { role: 'assistant', content: [{ type: 'thinking', thinking: 'hmm' }, { type: 'text', text: 'ok' }] },
]), { type: 'adaptive' });

assert.equal(pageFile('/'), '/index.html');
assert.equal(pageFile('/deploy'), '/deploy.html');
assert.equal(pageFile('/deploy/'), '/deploy.html');
assert.equal(pageFile('/index.html'), '/index.html');

console.log('web-ui serve: all checks pass');
