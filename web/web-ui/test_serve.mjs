#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Pure-unit checks for the drive v2 server helpers. Run: node web/web-ui/test_serve.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { shq, pathOk, parseFind, mimeFor, histTrim, histCloseOpenCalls, normHost, threadTurns, parseCaseUrl, liveCid, liveDestPath, livePathHasDotDot, tokenMatches, liveTarget, extraPlan, isLocalMode, pageFile, clip, snapshotElide, stashShot, hydrateShots, migrateShots, stashAttach, resolveAttach, hydrateAttaches, attachKind, ATTACH_MAX, sseEvents } from './serve.mjs';
import {
  CASE_TOOLS, chatAuth, resolveChatModel, openaiToolsToAnthropic,
  newAnthropicStreamCtx, anthropicEventToNdjson, tracesFromAnthropicMessage,
  histToAnthropicMessages, anthropicThinkingFor, caseToolPlan, withRateRetry,
} from './case-tools.mjs';

const html = fs.readFileSync(fileURLToPath(new URL('./index.html', import.meta.url)), 'utf8');
assert.match(html, /x-anthropic-key/);
assert.match(html, /ANTHROPIC KEY/);
assert.match(html, /claude-sonnet-4-6/);
assert.match(html, /id="attachStart"/);
assert.match(html, /id="attachPick"/);

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
assert.deepEqual(threadTurns([{ role: 'user', shot: '/tmp/x.png', content: [{ type: 'input_text', text: '[screenshot]' }] }]), [],
  'screenshots stay model-only on reopen');
assert.deepEqual(threadTurns([{ role: 'user', content: 'look', attaches: [{ name: 'invoice.pdf' }] }]),
  [{ who: 'you', text: 'look\ninvoice.pdf' }]);
assert.deepEqual(threadTurns([{ role: 'user', content: '', attaches: [{ name: 'notes.md' }] }]),
  [{ who: 'you', text: 'notes.md' }]);

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
const marked = extraPlan('computer_screenshot', { marks: true }, 'c_ab');
assert.ok(marked.rel.includes('marks=true'));
const hover = extraPlan('computer_hover', { ref: 3, name: 'Menu' }, 'c_ab');
assert.equal(hover.method, 'POST');
assert.equal(hover.rel, '/computers/c_ab/hover?wake=true');
const up = extraPlan('computer_upload', { ref: 2, path: '/home/agent/a.pdf' }, 'c_ab');
assert.equal(up.method, 'POST');
assert.equal(up.rel, '/computers/c_ab/upload?wake=true');
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
assert.equal(pageFile('/serve.mjs'), '');
assert.equal(pageFile('/threads.json'), '');
assert.equal(pageFile('/../secrets'), '');
assert.equal(pageFile('/deploy.html'), '/deploy.html');

{
  const msgs = histToAnthropicMessages([
    { role: 'user', content: [{ type: 'input_text', text: 'steer mid-turn' }] },
    { role: 'user', content: [{ type: 'input_image', detail: 'high', image_url: 'data:image/png;base64,xx' }] },
  ]);
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].content, 'steer mid-turn');
}
{
  const msgs = histToAnthropicMessages([
    { role: 'user', shot: '/tmp/x.png', content: [{ type: 'input_text', text: '[screenshot]' }] },
    { role: 'user', content: 'after the shot' },
  ]);
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].content, 'after the shot');
}

// STOP keeps the turn — rewind only on provider errors, not disconnect.
{
  const serveSrc = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  const caseToolsSrc = fs.readFileSync(fileURLToPath(new URL('./case-tools.mjs', import.meta.url)), 'utf8');
  const runTurnFn = serveSrc.slice(serveSrc.indexOf('export async function runTurn('), serveSrc.indexOf('async function chat('));
  const chatFn = serveSrc.slice(serveSrc.indexOf('async function chat('), serveSrc.indexOf('async function pendingHandoffIds('));
  const loopFn = runTurnFn + chatFn;
  assert.match(chatFn, /if \(stopped\(\) \|\| res\.destroyed\) return/);
  const outOfSteps = [...loopFn.matchAll(/if \(!finished(?: && !stopped\(\))?\)/g)];
  assert.equal(outOfSteps.length, 2, 'Anthropic + OpenAI out-of-steps gates');
  assert.ok(outOfSteps.every((m) => m[0].includes('!stopped()')), 'STOP is not out-of-rounds');
  assert.ok(!/turnStart/.test(loopFn), 'no turn rollback on provider error');
  assert.match(loopFn, /histCloseOpenCalls\(hist\.items\)/);
  assert.match(loopFn, /withRateRetry\(round, emit, 5, gone\.signal\)/);
  assert.match(chatFn, /res\.on\('close', \(\) => \{ clientGone\(\); gone\.abort\(\); \}\)/);
  assert.match(loopFn, /responses\.create\(params, \{ signal: rc\.signal \}\)/);
  assert.ok(!/responses\.create\(params\)/.test(loopFn), 'every round is abortable');
  assert.match(loopFn, /summary !== 'auto' && !gone\.signal\.aborted/, 'an abort never retries as a summary fallback');
  assert.match(loopFn, /gone\.signal\.removeEventListener\('abort', relay\)/, 'round listener is unlinked');
  assert.match(serveSrc, /takeSteers\(thread\.id\)/, 'steer inbox drained in the loop');
  assert.match(serveSrc, /type: 'steer'/, 'steer emits to the stream');
  assert.match(serveSrc, /beforeRound:/, 'anthropic loop drains steers each round');
  assert.match(serveSrc, /pushSteerItems\(thread\.items, leftover\)/, 'last-round steers persist with the turn');
  assert.match(loopFn, /prompt_cache_key: thread\.id/);
  assert.match(serveSrc, /CASE_TURN_TOKENS/);
  assert.match(loopFn, /tokenBudget: TURN_TOKEN_BUDGET/);
  assert.match(loopFn, /signal: gone\.signal/);
  assert.match(caseToolsSrc, /tokenBudget/);
  assert.match(caseToolsSrc, /stream\.abort\(\)/, 'Anthropic stream is canceled on disconnect');
  assert.match(caseToolsSrc, /withRateRetry\(\(\) => round\(params\), emit, 5, signal\)/);
  assert.match(loopFn, /if \(!stopped\(\)\) emit\(\{ type: 'error'/);
  assert.match(html, /\/api\/chat\/steer/);
  assert.match(html, /steerPrompt/);
  assert.match(loopFn, /eff=\$\{eff\}/, 'turn log reports billed tokens, not nominal');
  assert.match(serveSrc, /p === '\/api\/attach'/, 'user files land on disk, not in the chat body');
  assert.match(fs.readFileSync(fileURLToPath(new URL('./case-tools.mjs', import.meta.url)), 'utf8'),
    /cache_control: \{ type: 'ephemeral' \}/, 'Anthropic path requests prompt cache');
  assert.match(loopFn, /hydrateShots\(hydrateAttaches\(hist\.items\)\)/);
  assert.match(chatFn, /attachment not found/, 'a missing file is an error, not a silent drop');
  assert.ok(!/truncation:\s*['"]auto['"]/.test(loopFn), 'no truncation:auto');
  assert.ok(!/compactHistory|SUMMARIZE_PROMPT|CASE_COMPACT_AT/.test(serveSrc), 'no compaction');
  assert.match(serveSrc, /try \{ computerId = await cid\(\); \}/);
  assert.ok(!/drive ntfy chat on \$\{cfg\.url\}\/\$\{cfg\.topic\}/.test(serveSrc));
}

{
  assert.equal(clip('abc', 8000), 'abc');
  const long = 'A'.repeat(4000) + 'B'.repeat(4000) + '"truncated":true}';
  const cut = clip(long, 200);
  assert.ok(cut.startsWith('AAA'), 'head kept');
  assert.ok(cut.endsWith('"truncated":true}'), 'tail kept — this is what a blind cut lost');
  assert.match(cut, /chars elided/);
  assert.ok(cut.length < 300, 'stays near the budget');
  assert.equal(clip({ a: 1 }), '{"a":1}');
}

{
  const snaps = { last: '' };
  const snap = { ok: true, act: 'snapshot', result: { url: 'https://x/', elements: ['[1] button "Go"'] } };
  assert.deepEqual(snapshotElide('computer_snapshot', snap, snaps), snap, 'first snapshot passes through');
  const again = snapshotElide('computer_snapshot', JSON.parse(JSON.stringify(snap)), snaps);
  assert.equal(again.result.unchanged, true);
  assert.equal(again.result.url, 'https://x/');
  assert.ok(!again.result.elements, 'element list dropped');
  const moved = { ok: true, act: 'snapshot', result: { url: 'https://x/2', elements: ['[1] link "Next"'] } };
  assert.deepEqual(snapshotElide('computer_snapshot', moved, snaps), moved, 'changed page passes through');
  const other = { ok: true, act: 'eval', result: { value: 1 } };
  assert.deepEqual(snapshotElide('computer_eval', other, snaps), other, 'only snapshots are elided');
  const failed = { ok: false, act: 'snapshot', error: 'boom' };
  assert.deepEqual(snapshotElide('computer_snapshot', failed, snaps), failed, 'errors pass through');
}

{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-shots-'));
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
  const item = stashShot(png, dir);
  assert.ok(item.shot.startsWith(dir));
  assert.ok(item.shot.endsWith('.png'));
  assert.ok(fs.existsSync(item.shot));
  assert.ok(JSON.stringify(item).length < 400, 'the history item is a pointer, not the png');
  const hydrated = hydrateShots([item], dir);
  assert.equal(hydrated[0].content[0].type, 'input_image');
  assert.equal(hydrated[0].content[0].detail, 'high');
  assert.ok(hydrated[0].content[0].image_url.startsWith('data:image/png;base64,'));
  const missing = hydrateShots([{ role: 'user', shot: path.join(dir, 'nope.png'), content: [{ type: 'input_text', text: '[screenshot]' }] }], dir);
  assert.equal(missing[0].content[0].type, 'input_text');
  const outside = hydrateShots([{ role: 'user', shot: '/etc/passwd', content: [{ type: 'input_text', text: '[screenshot]' }] }], dir);
  assert.equal(outside[0].content[0].type, 'input_text', 'paths outside the shots dir are refused');
  const legacy = [{ role: 'user', content: [{ type: 'input_image', detail: 'high', image_url: 'data:image/png;base64,' + png }] }];
  const moved = migrateShots(legacy, dir);
  assert.ok(moved[0].shot);
  assert.notEqual(moved, legacy);
  assert.equal(migrateShots(moved, dir), moved, 'already migrated — no second write');
  fs.rmSync(dir, { recursive: true, force: true });
}

{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-inbox-'));
  const pngB64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
  const png = Buffer.from(pngB64, 'base64');
  assert.equal(attachKind('image/png'), 'image');
  assert.equal(attachKind('application/octet-stream', 'x.exe'), '');
  assert.throws(() => stashAttach(Buffer.from('MZ'), 'bad.exe', 'application/octet-stream', dir), /not allowed/);
  assert.throws(() => stashAttach(Buffer.alloc(ATTACH_MAX + 1), 'big.txt', 'text/plain', dir), /too large/);
  const img = stashAttach(png, 'dot.png', 'image/png', dir);
  assert.ok(fs.existsSync(img.path));
  assert.ok(JSON.stringify(img).length < 400, 'the record is a pointer');
  const notes = stashAttach(Buffer.from('hello notes', 'utf8'), 'notes.md', 'text/plain', dir);
  const dotted = stashAttach(Buffer.from('final report', 'utf8'), 'report..final.md', 'text/plain', dir);
  assert.ok(dotted.name.includes('..'), 'dots in the name survive');
  assert.ok(resolveAttach(dotted.id, dir), 'an id with .. in the filename still resolves');
  assert.equal(resolveAttach('../etc/passwd', dir), null);
  assert.equal(resolveAttach('..', dir), null);
  const blocker = path.join(os.tmpdir(), 'case-inbox-not-a-dir');
  fs.writeFileSync(blocker, 'x');
  assert.throws(() => stashAttach(Buffer.from('hi'), 'notes.md', 'text/plain', blocker), /could not store/);
  fs.rmSync(blocker);
  const hyd = hydrateAttaches([
    { role: 'user', content: 'see these', attaches: [
      { path: img.path, name: img.name, mime: img.mime },
      { path: notes.path, name: notes.name, mime: notes.mime },
    ] },
  ], dir);
  assert.equal(hyd[0].content[0].type, 'input_text');
  assert.equal(hyd[0].content[0].text, 'see these');
  assert.equal(hyd[0].content[1].type, 'input_image');
  assert.ok(hyd[0].content[1].image_url.startsWith('data:image/png;base64,'));
  assert.equal(hyd[0].content[2].type, 'input_text');
  assert.match(hyd[0].content[2].text, /notes\.md/);
  assert.match(hyd[0].content[2].text, /hello notes/);
  const missing = hydrateAttaches([{
    role: 'user', content: '',
    attaches: [{ path: path.join(dir, 'nope.md'), name: 'gone.md', mime: 'text/plain' }],
  }], dir);
  assert.equal(missing[0].content[0].text, '[gone.md]');
  const outside = hydrateAttaches([{
    role: 'user', content: '',
    attaches: [{ path: '/etc/passwd', name: 'passwd', mime: 'text/plain' }],
  }], dir);
  assert.equal(outside[0].content[0].text, '[passwd]', 'paths outside the inbox are refused');
  const msgs = histToAnthropicMessages(hyd, { media: true });
  assert.equal(msgs[0].role, 'user');
  assert.ok(Array.isArray(msgs[0].content));
  assert.equal(msgs[0].content.find((p) => p.type === 'image').source.media_type, 'image/png');
  const silent = histToAnthropicMessages(hyd);
  assert.equal(typeof silent[0].content, 'string');
  assert.ok(!JSON.stringify(silent).includes('image'));
  fs.rmSync(dir, { recursive: true, force: true });
}

{
  let n = 0;
  const out = await withRateRetry(async () => {
    n += 1;
    if (n < 3) {
      const err = new Error('rate limit: try again in 0s');
      err.status = 429;
      throw err;
    }
    return 'ok';
  }, () => {}, 5);
  assert.equal(out, 'ok');
  assert.equal(n, 3);
}

{
  const ctl = new AbortController();
  let n = 0;
  const started = Date.now();
  await assert.rejects(
    withRateRetry(async () => {
      n += 1;
      const err = new Error('rate limited');
      err.status = 429;
      throw err;
    }, () => ctl.abort(), 5, ctl.signal),
    (err) => err?.name === 'AbortError',
  );
  assert.equal(n, 1, 'disconnect stops retries before another provider request');
  assert.ok(Date.now() - started < 500, 'disconnect interrupts the backoff sleep');
}

{
  const run = (cmd) => {
    const plan = caseToolPlan('computer_exec', { command: cmd }, 'c_1');
    return { out: execFileSync('bash', ['-c', plan.json.command], { encoding: 'utf8' }), log: plan.logPath };
  };
  const a = run('printf "one\\ntwo\\n"; exit 3');
  assert.match(a.out, /exit=3/, 'exit code survives the redirect');
  assert.match(a.out, /lines=2/);
  assert.match(a.out, /one\ntwo/);
  const b = run('echo oops >&2');
  assert.match(b.out, /exit=0/);
  assert.match(b.out, /oops/, 'stderr is captured too');
  const c = run('seq 1 100000');
  assert.match(c.out, /lines=100000/);
  assert.ok(c.out.length < 3000, 'head only, not 100k lines of history');
  assert.match(fs.readFileSync(c.log, 'utf8'), /\n100000\n$/, 'the file has the rest');
  assert.match(run('cd /usr && pwd').out, /\/usr/, 'the command still runs in one shell');
  for (const { log } of [a, b, c]) fs.rmSync(log, { force: true });
}

{
  const { events, rest } = sseEvents(
    ': connected\n\n'
    + 'event: handoff_created\ndata: {"id":"h_1","kind":"approval"}\n\n'
    + ': hb\n\n'
    + 'event: credential_added\ndata: {"name":"x"}\n\nevent: handoff_cre',
  );
  assert.deepEqual(events, [
    { event: 'handoff_created', data: { id: 'h_1', kind: 'approval' } },
    { event: 'credential_added', data: { name: 'x' } },
  ]);
  assert.equal(rest, 'event: handoff_cre');
}

console.log('web-ui serve: all checks pass');
