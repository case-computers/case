#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Pure-unit checks for the drive v2 server helpers. Run: node web/web-ui/test_serve.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  CASE_TOOLS, chatAuth, resolveChatModel, resolveChatEffort, effortsForModel, openaiToolsToAnthropic,
  newAnthropicStreamCtx, anthropicEventToNdjson, tracesFromAnthropicMessage,
  histToAnthropicMessages, anthropicThinkingFor, caseToolPlan, clip,
} from './case-tools.mjs';
import { putProviderKey } from './provider-secrets.mjs';

// serve.mjs loads threads.json at import and rewrites it; never the developer's own.
process.env.CASE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'case-home-'));
process.env.CASE_THREADS = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'case-threads-')), 'threads.json');
process.env.CASE_TURN_TOKENS = '10000';   // small enough for the scripted turns below to cross 80%
const { runTurn, brainAuth, chatBrainAuth, ALL_TOOLS, shq, pathOk, parseErr, parseFind, mimeFor, histTrim, histApplyCompaction, histCloseOpenCalls, normHost, threadTurns, parseCaseUrl, liveCid, liveDestPath, livePathHasDotDot, tokenMatches, liveHeaders, hostOf, browserOk, extraPlan, pageFile, appFile, snapshotElide, stashShot, pushShot, hydrateShots, migrateShots, stashAttach, resolveAttach, hydrateAttaches, attachKind, ATTACH_MAX, sseEvents } = await import('./serve.mjs');

const html = fs.readFileSync(fileURLToPath(new URL('./index.html', import.meta.url)), 'utf8');
assert.match(html, /x-anthropic-key/);
assert.match(html, /newest key saved on the box or the ChatGPT login/);
assert.doesNotMatch(html, /Uses CASE_DRIVE_API_KEY from \.env, not the tab KEY/);
assert.match(html, /id="keyTabChatgpt"/);
assert.match(html, /fetch\('\/api\/chatgpt\/login'/);
assert.match(html, /provider==='chatgpt'\?\{\}/, 'the ChatGPT brain sends no tab key');
assert.match(html, /ANTHROPIC KEY/);
assert.match(html, /claude-sonnet-5-5/);
assert.match(html, /\{id:'claude-sonnet-5-5',name:'Sonnet 5\.5'/);
assert.match(html, /\{id:'claude-opus-5-5',name:'Opus 5\.5'/);
assert.match(html, /\{id:'claude-haiku-5-5',name:'Haiku 5\.5'/);
assert.match(html, /\{id:'gpt-6-astra',name:'GPT-6 Astra'/);
assert.match(html, /\{id:'gpt-6\.1-sol',name:'GPT-6\.1 Sol'/);
assert.match(html, /\{id:'gpt-6-luna',name:'GPT-6 Luna'/);
assert.match(html, /function nudgeDesk\(/);
assert.match(html, /if\(wasHidden\)\{\s*wasHidden=false;\s*nudgeDesk\(\)/);
assert.ok(!/if\(wasHidden\)\{wasHidden=false;reconnectDesk\(\)\}/.test(html), 'brief tab hide must not tear down a live desk');
assert.match(html, /addEventListener\('paste'/);
assert.match(html, /addEventListener\('drop'/);
assert.match(html, /id="attachStart"/);
assert.match(html, /id="attachPick"/);
assert.match(html, /id="schedModal"/);
assert.match(html, /id="schedTz"/);
assert.match(html, /id="schedBtn"/);
assert.match(html, /id="schedBtnM"/);
assert.match(html, /rel="manifest"/);
assert.match(html, /viewport-fit=cover/);
assert.match(html, /serviceWorker.*register\('\/sw\.js'/s);
assert.match(html, /id="tabbar"/);
assert.match(html, /id="tabScreen"/);
assert.match(html, /id="takeover"/);
assert.match(html, /id="tray"/);
assert.match(html, /--motion-travel/);
assert.match(html, /\/api\/shot\?h=/);
assert.match(html, /\/api\/handoffs\/answer/);
assert.match(html, /\/api\/push/);
assert.match(html, /your phone buzzes and you type it in/);
assert.match(html, /\/api\/chat\/stop/);
assert.match(html, /userStopped/);
assert.match(html, /streamDrop/);
assert.match(html, /resumeBackground/);
assert.match(html, /if\(tab==='screen'\)\{hookDesk\(\)/);
assert.match(html, /h\.kind==='otp'\?'<button type="button" class="btn" id="hoDone">/, 'the code form offers DONE only for otp');
assert.match(html, /String\(j\.error\|\|'COULD NOT SEND'\)\.toUpperCase\(\)/, 'a failed answer shows the server error');
const manifest = JSON.parse(fs.readFileSync(fileURLToPath(new URL('./manifest.json', import.meta.url)), 'utf8'));
assert.equal(manifest.start_url, '/?app=1');
assert.equal(manifest.display, 'standalone');
const sw = fs.readFileSync(fileURLToPath(new URL('./sw.js', import.meta.url)), 'utf8');
assert.match(sw, /addEventListener\('push'/);
assert.match(sw, /notificationclick/);
assert.ok(!/addEventListener\('fetch'/.test(sw), 'no offline cache: Drive is a live view');
assert.match(html, /Intl\.supportedValuesOf/);
assert.match(html, /brain_key/);
for (const m of html.matchAll(/innerHTML=[^\n]*apiErr\(j\)[^\n]*/g)) assert.match(m[0], /esc\(apiErr\(j\)/, 'error text is escaped before innerHTML');
for (const m of html.matchAll(/'\/api\/(fs|file|creds|schedules|teach-tick)[^;]*/g)) {
  assert.match(m[0], /compQ\(\)/, `${m[1]} names the picked computer: ${m[0]}`);
}
assert.match(fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8'), /schedulesRoute/);
{
  const src = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  assert.match(src, /p === '\/api\/shot'/);
  assert.match(src, /p === '\/api\/push'/);
  assert.match(src, /p === '\/api\/handoffs'/);
  assert.match(src, /p === '\/api\/handoffs\/answer'/);
  assert.match(src, /p === '\/api\/handoffs\/shot'/);
  assert.match(src, /p === '\/api\/chat\/stop'/);
  assert.match(src, /schedule\.run/);
  assert.match(src, /filter\(\(it\) => !it\?\.card\)/, 'card items never reach a model');
}

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
assert.equal(view[1].ok, true);
// a reopened thread shows which tools failed, not "ok" for all of them
{
  const t = threadTurns([
    { type: 'function_call', call_id: 'a', name: 'computer_click_element', arguments: '{}' },
    { type: 'function_call_output', call_id: 'a', output: '{"ok":false,"status":409,"error":"stale ref","act":"click [3]"}' },
    { type: 'function_call', call_id: 'b', name: 'computer_login', arguments: '{}' },
    { type: 'function_call_output', call_id: 'b', output: '{"ok":false,"error":"interrupted"}' },
    { type: 'function_call', call_id: 'c', name: 'computer_snapshot', arguments: '{}' },
    { type: 'function_call_output', call_id: 'c', output: clip({ ok: true, act: 'snapshot', result: { elements: ['x'.repeat(9000)] } }) },
  ]);
  assert.deepEqual(t.map((x) => x.ok), [false, false, true]);
}
assert.deepEqual(threadTurns([{ role: 'user', shot: '/tmp/x.png', content: [{ type: 'input_text', text: '[screenshot]' }] }]), [],
  'screenshots stay model-only on reopen');
assert.deepEqual(threadTurns([{ role: 'user', content: 'look', attaches: [{ name: 'invoice.pdf' }] }]),
  [{ who: 'you', text: 'look\ninvoice.pdf' }]);
assert.deepEqual(threadTurns([{ role: 'user', content: '', attaches: [{ name: 'notes.md' }] }]),
  [{ who: 'you', text: 'notes.md' }]);
const withCard = threadTurns([
  { role: 'user', content: 'go' },
  { role: 'shotcard', card: 1, at: 'turn_end', h: 'b'.repeat(40), shot: '/x/b.png', ts: 5 },
]);
assert.deepEqual(withCard.map((t) => t.who), ['you', 'shot']);
assert.deepEqual(hydrateAttaches([{ role: 'shotcard', card: 1, shot: '/x/b.png' }, { role: 'user', content: 'go' }]), [{ role: 'user', content: 'go' }]);

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

assert.equal(parseErr(Buffer.from('{"error":{"message":"nope"}}'), 'read failed'), 'nope');
assert.equal(parseErr(Buffer.from('<html>502</html>'), 'read failed'), 'read failed');

// threads.json is replaced, never half-written; a stale one must not vanish silently.
{
  const src = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  assert.match(src, /renameSync\(THREADS_FILE \+ '\.tmp', THREADS_FILE\)/);
  assert.match(src, /console\.warn\('threads:', err\.message\)/);
  assert.ok(!/fonts\.(googleapis|gstatic)/.test(html), 'no font CDN on the page');
}

// histTrim: over budget, old turns collapse to [prompt, last reply] — the task and
// the model's own "stopped at X" survive, tool observations die. Never an orphan
// function_call / output (either kind 400s every later request).
const reply = (n) => ({ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: `did ${n}` }] });
const turn = (n, pad) => [
  { role: 'user', content: `ask ${n}${pad}` },
  { type: 'function_call', call_id: `c${n}`, name: 'computer_snapshot', arguments: '{}' },
  { type: 'function_call_output', call_id: `c${n}`, output: 'ok' + pad },
  reply(n),
];
const h = { items: [...turn(1, 'x'.repeat(400)), ...turn(2, ''), ...turn(3, '')] };
histTrim(h, 1200);   // 1602 chars; collapsing turn 1 leaves 1057
assert.deepEqual(h.items.map((i) => i.call_id || (Array.isArray(i.content) ? i.content[0].text : i.content)),
  ['ask 1' + 'x'.repeat(400), 'did 1', 'ask 2', 'c2', 'c2', 'did 2', 'ask 3', 'c3', 'c3', 'did 3']);
for (const it of h.items.filter((i) => i.type === 'function_call')) {
  assert.ok(h.items.some((o) => o.type === 'function_call_output' && o.call_id === it.call_id),
    `call ${it.call_id} lost its output`);
}
// screenshots, steers and notes are role:user too — they are NOT turn boundaries.
// The cut must never land inside a turn and take the task with it.
const tmpShots = fs.mkdtempSync(path.join(os.tmpdir(), 'shots-'));
const shotTurn = { items: [
  { role: 'user', content: 'ORIGINAL TASK' },
  { type: 'function_call', call_id: 'c1', name: 'computer_navigate', arguments: '{}' },
  { type: 'function_call_output', call_id: 'c1', output: 'x'.repeat(3000) },
  stashShot('aGVsbG8=', tmpShots),
  { role: 'user', content: [{ type: 'input_text', text: 'steer: faster' }] },
  { type: 'function_call', call_id: 'c2', name: 'computer_click', arguments: '{}' },
  { type: 'function_call_output', call_id: 'c2', output: 'ok' },
  reply('shot'),
  { role: 'user', content: 'continue' },
  { type: 'function_call', call_id: 'c3', name: 'computer_click', arguments: '{}' },
  { type: 'function_call_output', call_id: 'c3', output: 'ok' },
] };
histTrim(shotTurn, 500);
assert.deepEqual(shotTurn.items.map((i) => i.call_id || (Array.isArray(i.content) ? i.content[0].text : i.content)),
  ['ORIGINAL TASK', 'did shot', 'continue', 'c3', 'c3']);
fs.rmSync(tmpShots, { recursive: true, force: true });
// still over after every turn is collapsed: drop the oldest collapsed ones. The
// newest turn keeps its tail (small enough to fit TAIL_KEEP whole).
const many = { items: [...turn(1, 'x'.repeat(300)), ...turn(2, 'y'.repeat(300)), ...turn(3, '')] };
histTrim(many, 800);
assert.deepEqual(many.items.map((i) => i.call_id || (Array.isArray(i.content) ? i.content[0].text : i.content)),
  ['ask 2' + 'y'.repeat(300), 'did 2', 'ask 3', 'c3', 'c3', 'did 3']);
// A lone giant turn collapses too: histTrim only runs after a turn has ended, and
// left whole it is what every round of the NEXT turn re-sends. It keeps its prompt
// and a bounded tail, so "continue" resumes with real observations rather than blind.
const giant = { items: [{ role: 'user', content: 'THE TASK' }] };
for (let n = 0; n < 60; n++) {
  giant.items.push({ type: 'function_call', call_id: `g${n}`, name: 'computer_eval', arguments: '{}' });
  giant.items.push({ type: 'function_call_output', call_id: `g${n}`, output: 'z'.repeat(4000) });
}
giant.items.push(reply('giant'));
const wasBig = JSON.stringify(giant.items).length;
histTrim(giant);
const nowSmall = JSON.stringify(giant.items).length;
assert.ok(wasBig > 240_000 && nowSmall < 60_000, `collapsed ${wasBig} -> ${nowSmall}`);
assert.equal(giant.items[0].content, 'THE TASK', 'the task survives its own turn collapsing');
assert.ok(giant.items.length > 3, 'a tail of real observations survives, not just the prompt');
const tailCalls = new Set(giant.items.filter((i) => i.type === 'function_call').map((i) => i.call_id));
for (const it of giant.items.filter((i) => i.type === 'function_call_output')) {
  assert.ok(tailCalls.has(it.call_id), `orphan output ${it.call_id} would 400 every later request`);
}
// Batched calls: two calls then two outputs. A suffix cut that keeps `kept`'s
// call plus `old`'s output (but not `old`'s call) used to leave that output
// in the middle of the tail — leading-only stripping missed it.
const batched = { items: [
  { role: 'user', content: 'TASK' },
  { type: 'function_call', call_id: 'old', name: 'computer_eval', arguments: 'x'.repeat(5000) },
  { type: 'function_call', call_id: 'kept', name: 'computer_eval', arguments: '{}' },
  { type: 'function_call_output', call_id: 'old', output: 'z'.repeat(35000) },
  { type: 'function_call_output', call_id: 'kept', output: 'ok' },
  reply('batched'),
] };
histTrim(batched, 1000);
const batchedCalls = new Set(batched.items.filter((i) => i.type === 'function_call').map((i) => i.call_id));
for (const it of batched.items.filter((i) => i.type === 'function_call_output')) {
  assert.ok(batchedCalls.has(it.call_id), `orphan output ${it.call_id} would 400 every later request`);
}
assert.ok(!batched.items.some((i) => i.call_id === 'old'),
  'a cut-away batched call must not leave its output in the tail');
histTrim(giant, 10);
assert.equal(giant.items[0].content, 'THE TASK');
const small = { items: turn(1, '') };
histTrim(small, 100000);
assert.equal(small.items.length, 4);

// histApplyCompaction: after a server-side compaction item, everything before it
// goes except the turn openers — the task survives, the compaction carries the rest.
const comp = histApplyCompaction([
  { role: 'user', content: 'task A' },
  { type: 'function_call', call_id: 'a1', name: 'computer_snapshot', arguments: '{}' },
  { type: 'function_call_output', call_id: 'a1', output: 'big' },
  { role: 'user', content: 'continue' },
  { type: 'function_call', call_id: 'b1', name: 'computer_snapshot', arguments: '{}' },
  { type: 'function_call_output', call_id: 'b1', output: 'big' },
  { role: 'user', content: [{ type: 'input_text', text: 'steer' }] },
  { type: 'compaction', id: 'cmp_1', encrypted_content: 'opaque' },
  { type: 'function_call', call_id: 'b2', name: 'computer_click', arguments: '{}' },
  { type: 'function_call_output', call_id: 'b2', output: 'ok' },
]);
assert.deepEqual(comp.map((i) => i.call_id || i.type || i.content), ['task A', 'continue', 'compaction', 'b2', 'b2']);
assert.deepEqual(histApplyCompaction(small.items), small.items, 'no compaction item: untouched');

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

{
  const h = liveHeaders({ headers: { cookie: 'case_token=s', accept: '*/*' } }, false, 'tok');
  assert.equal(h.authorization, 'Bearer tok');
  assert.equal(h.cookie, undefined);
  assert.equal(h.accept, '*/*');
}

assert.equal(hostOf('[::1]:4174'), '[::1]');
assert.equal(hostOf('127.0.0.1:4174'), '127.0.0.1');
assert.ok(browserOk({ headers: { host: '[::1]:4174' } }));
assert.ok(browserOk({ headers: { host: 'localhost:4174' } }), 'no Origin is a same-site navigation');
assert.ok(browserOk({ headers: { host: 'localhost:4174', origin: 'http://127.0.0.1:4174' } }));
assert.ok(!browserOk({ headers: { host: 'localhost:4174', origin: 'https://evil.example' } }));
assert.ok(!browserOk({ headers: { host: 'evil.example' } }), 'DNS rebinding');

// A file off the computer is a download, never a page.
{
  const src = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  const fn = src.slice(src.indexOf('async function fsFile('), src.indexOf('async function creds('));
  assert.match(fn, /'application\/octet-stream'/);
  assert.match(fn, /'x-content-type-options': 'nosniff'/);
  assert.ok(!/svg/.test(html.match(/const IMG_RE=[^\n]*/)[0]), 'svg previews as text, not as an image');
}

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
const hw = extraPlan('handoff_wait', { handoff_id: 'h_1', max_wait_s: 60 }, 'c_ab');
assert.equal(hw.method, 'GET');
assert.equal(hw.rel, '/handoffs/h_1/wait?after_status=pending&timeout_s=60');
assert.equal(hw.timeoutMs, 70000);
assert.equal(extraPlan('handoff_wait', { handoff_id: 'h_1', max_wait_s: 1.5 }, 'c_ab').rel,
  '/handoffs/h_1/wait?after_status=pending&timeout_s=1');
assert.equal(extraPlan('handoff_list', {}, 'c_ab').rel, '/handoffs?status=pending');
assert.equal(extraPlan('handoff_get', { handoff_id: 'h_1' }, 'c_ab').rel, '/handoffs/h_1');
assert.equal(caseToolPlan('computer_navigate', { url: 'https://x', target_id: 'tab1' }, 'c_ab').json.target_id, 'tab1');
assert.equal(caseToolPlan('computer_eval', { expression: '1', target_id: 'tab1' }, 'c_ab').json.target_id, 'tab1');
assert.match(extraPlan('computer_snapshot', { target_id: 'tab1' }, 'c_ab').rel, /[?&]target_id=tab1$/);
assert.equal(extraPlan('computer_click_element', { ref: 1, target_id: 'tab1' }, 'c_ab').body.target_id, 'tab1');
assert.equal(extraPlan('computer_fill', { fields: [], target_id: 'tab1' }, 'c_ab').body.target_id, 'tab1');
assert.equal(extraPlan('computer_capture_start', { url_pattern: 'x', target_id: 'tab1' }, 'c_ab').body.target_id, 'tab1');
assert.equal(extraPlan('computer_list', {}, 'c_ab').rel, '/computers');
assert.equal(extraPlan('computer_create', { name: 'desk' }, 'c_ab').method, 'POST');
assert.deepEqual(extraPlan('computer_create', { name: 'desk' }, 'c_ab').body, { name: 'desk' });

assert.deepEqual(chatAuth({ 'x-openai-key': 'sk-openai' }), { provider: 'openai', key: 'sk-openai' });
assert.deepEqual(chatAuth({ 'x-anthropic-key': 'sk-ant-test' }), { provider: 'anthropic', key: 'sk-ant-test' });
assert.deepEqual(chatAuth({}), { provider: '', key: '' });
assert.equal(chatAuth({ 'x-anthropic-key': 'sk-ant-test', 'x-openai-key': 'sk-openai' }).provider, 'anthropic');
assert.equal(resolveChatModel('gpt-6-luna', 'openai'), 'gpt-6-luna');
assert.equal(resolveChatModel('gpt-6-astra', 'openai'), 'gpt-6-astra');
assert.equal(resolveChatModel('gpt-6.1-sol', 'openai'), 'gpt-6.1-sol');
assert.equal(resolveChatModel('nope', 'openai'), 'gpt-6-luna');
assert.equal(resolveChatModel('claude-opus-5-5', 'anthropic'), 'claude-opus-5-5');
assert.equal(resolveChatModel('claude-sonnet-5-5', 'anthropic'), 'claude-sonnet-5-5');
assert.equal(resolveChatModel('claude-haiku-5-5', 'anthropic'), 'claude-haiku-5-5');
assert.equal(resolveChatModel('gpt-6.1-sol', 'anthropic'), 'claude-sonnet-5-5');
assert.deepEqual(effortsForModel('gpt-6-astra'), ['low', 'medium', 'high', 'xhigh', 'max']);
assert.deepEqual(effortsForModel('gpt-6.1-sol'), ['low', 'medium', 'high', 'xhigh', 'max']);
assert.deepEqual(effortsForModel('gpt-6-luna'), ['none', 'low', 'medium', 'high', 'xhigh', 'max']);
assert.deepEqual(effortsForModel('claude-opus-5-5'), ['low', 'medium', 'high', 'xhigh', 'max']);
assert.equal(resolveChatEffort('gpt-6-astra', 'none'), 'low');
assert.equal(resolveChatEffort('gpt-6.1-sol', 'none'), 'low');
assert.equal(resolveChatEffort('gpt-6-luna', 'none'), 'none');
assert.equal(resolveChatEffort('claude-sonnet-5-5', 'xhigh'), 'xhigh');
assert.equal(resolveChatEffort('gpt-6-astra', 'bogus'), 'high');
assert.match(html, /effort==='none'&&allowed\.includes\('low'\)\?'low'/,
  'switching off Luna none clamps to low like the server, not high');
{
  const tools = openaiToolsToAnthropic(CASE_TOOLS);
  assert.equal(tools[0].name, 'computer_navigate');
  // caseToolPlan forwards button, so the schema has to let the model send it
  const action = CASE_TOOLS.find((t) => t.name === 'computer_action').parameters.properties;
  assert.deepEqual(action.button.enum, ['left', 'middle', 'right']);
  assert.equal(caseToolPlan('computer_action', { type: 'click', x: 1, y: 2, button: 'right' }, 'c_1').json.button, 'right');
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
assert.equal(appFile('/manifest.json'), '/manifest.json');
assert.equal(appFile('/sw.js'), '/sw.js');
assert.equal(appFile('/icons/icon-192.png'), '/icons/icon-192.png');
assert.equal(appFile('/icons/../serve.mjs'), '');
assert.equal(appFile('/threads.json'), '');

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

// With media, a hydrated shot reaches the model as an image, not as a dropped item.
{
  const shot = { role: 'user', shot: '/tmp/x.png', content: [{ type: 'input_image', detail: 'high', image_url: 'data:image/png;base64,xx' }] };
  const msgs = histToAnthropicMessages([shot], { media: true });
  assert.equal(msgs.length, 1);
  assert.deepEqual(msgs[0].content, [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'xx' } }]);
}

// The Anthropic tool_result carries the png itself; a repeat says so instead.
{
  const src = fs.readFileSync(fileURLToPath(new URL('./case-tools.mjs', import.meta.url)), 'utf8');
  assert.match(src, /content\.push\(\{ type: 'image', source: \{ type: 'base64', media_type: 'image\/png', data: image_b64 \} \}\)/);
  // live tool_result and persisted history are cut by the same head+tail clip
  assert.match(src, /\{ type: 'text', text: clip\(rest\) \}/);
  assert.ok(!/clipJson/.test(src));
  const items = [];
  const shots = new Set();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-dedupe-'));
  pushShot(items, shots, 'AAAA', dir);
  pushShot(items, shots, 'AAAA', dir);
  assert.equal(items.length, 3);
  assert.ok(items[0].shot, 'first shot is stashed');
  assert.equal(items[1].card, 1, 'the first shot also lands as a card for the human');
  assert.match(items[2].content[0].text, /has not changed/);
}

// STOP keeps the turn — rewind only on provider errors, not disconnect.
{
  const serveSrc = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  const caseToolsSrc = fs.readFileSync(fileURLToPath(new URL('./case-tools.mjs', import.meta.url)), 'utf8');
  const runTurnFn = serveSrc.slice(serveSrc.indexOf('export async function runTurn('), serveSrc.indexOf('async function chat('));
  const chatFn = serveSrc.slice(serveSrc.indexOf('async function chat('), serveSrc.indexOf('async function pendingHandoffIds('));
  const loopFn = runTurnFn + chatFn;
  assert.match(chatFn, /if \(res\.destroyed\) return/);
  assert.match(chatFn, /CHAT_STOP\.set\(thread\.id, gone\)/);
  const outOfSteps = [...loopFn.matchAll(/if \(!finished(?: && !stopped\(\))?\)/g)];
  assert.equal(outOfSteps.length, 2, 'Anthropic + OpenAI out-of-steps gates');
  assert.ok(outOfSteps.every((m) => m[0].includes('!stopped()')), 'STOP is not out-of-rounds');
  assert.ok(!/turnStart/.test(loopFn), 'no turn rollback on provider error');
  assert.match(loopFn, /finishTurn\(hist, thread\)/);
  assert.match(serveSrc, /function finishTurn[\s\S]*?histCloseOpenCalls\(hist\.items\)/);
  assert.match(loopFn, /withRateRetry\(round, emit, 5, gone\.signal\)/);
  assert.match(loopFn, /Look before you route/, 'dev prompt says act on the page before trying another url');
  assert.match(loopFn, /handoff_request\(kind=device\)/, 'dev prompt names the human escape hatch');
  assert.match(chatFn, /async function chatStop/);
  assert.match(chatFn, /running\.length === 1/);
  assert.ok(!/res\.on\('close'/.test(chatFn), 'a hangup does not abort the running turn');
  assert.match(loopFn, /responses\.create\(params, \{ signal: rc\.signal \}\)/);
  assert.ok(!/responses\.create\(params\)/.test(loopFn), 'every round is abortable');
  assert.match(loopFn, /if \(gone\.signal\.aborted\) throw err;/, 'an abort never retries as a param fallback');
  assert.match(loopFn, /if \(isRetryable\(err\)\) throw err;/);
  assert.match(loopFn, /else if \(summary !== 'auto'\)/);
  assert.ok(loopFn.lastIndexOf('histApplyCompaction') > loopFn.indexOf("histCloseOpenCalls(hist.items, { keepReasoning: true })"),
    'the tool round applies compaction only after closing its open calls');
  assert.equal([...loopFn.matchAll(/histApplyCompaction/g)].length, 2, 'both the answering and tool rounds compact');
  assert.match(serveSrc, /\.filter\(\(it\) => !isBudgetWarn\(it\)\)/, 'the budget warning does not outlive its turn');
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
  assert.match(html, /const setText=t=>\{raw=t;if\(!textFrame\)textFrame=requestAnimationFrame\(drawText\);\};/,
    'a text_delta does not re-parse the whole reply');
  assert.match(html, /if\(textFrame\)\{cancelAnimationFrame\(textFrame\);drawText\(\);\}\n    caret\.remove\(\);/,
    'the last frame is drawn before the caret goes');
  assert.match(html, /if\(nav!==navDrawn\)/, 'refresh repaints the sidebar only when it changed');
  // switching threads mid-turn: a slow reopen never paints over a newer pick
  // (where typed messages go is covered in test_nav.mjs)
  assert.match(html, /if\(gen!==threadGen\)return;/);
  assert.match(html, /const tid=steerTarget\(runTid,activeTid\);/);
  assert.match(html, /ev\.type==='round'\)\{mark=/, 'the UI marks where each provider round began');
  assert.match(html, /ev\.type==='round_reset'&&mark/, 'and drops a failed round\'s partial output on replay');
  assert.match(caseToolsSrc, /emit\(\{ type: 'round_reset' \}\);\n      result = await withRateRetry\(\(\) => round\(rest\)/,
    'the output_config fallback replays the round too');
  assert.match(loopFn, /eff=\$\{eff\}/, 'turn log reports billed tokens, not nominal');
  assert.match(serveSrc, /p === '\/api\/attach'/, 'user files land on disk, not in the chat body');
  assert.match(fs.readFileSync(fileURLToPath(new URL('./case-tools.mjs', import.meta.url)), 'utf8'),
    /cache_control: \{ type: 'ephemeral' \}/, 'Anthropic path requests prompt cache');
  assert.match(loopFn, /hydrateShots\(hydrateAttaches\(hist\.items\)\)/);
  assert.match(chatFn, /attachment not found/, 'a missing file is an error, not a silent drop');
  assert.match(loopFn, /truncation: 'auto'/);
  assert.match(serveSrc, /CASE_COMPACT_AT/);
  assert.ok(!/compactHistory|SUMMARIZE_PROMPT/.test(serveSrc), 'no client-side summarizer');
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
  // A turn reads each file once: with its memo, a later round needs no disk.
  const memo = new Map();
  const first = hydrateShots([item], dir, memo)[0];
  const copy = path.join(dir, 'keep.png');
  fs.renameSync(item.shot, copy);
  assert.equal(hydrateShots([item], dir, memo)[0], first);
  assert.equal(hydrateShots([item], dir)[0].content[0].type, 'input_text', 'without it the file is read again');
  fs.renameSync(copy, item.shot);
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
  {
    const memo = new Map();
    const turn = [{ role: 'user', content: '', attaches: [{ path: img.path, name: img.name, mime: img.mime }] }];
    const once = hydrateAttaches(turn, dir, memo);
    fs.renameSync(img.path, img.path + '.moved');
    const again = hydrateAttaches(turn, dir, memo);
    fs.renameSync(img.path + '.moved', img.path);
    assert.deepEqual(again, once, 'an attachment is read once per turn');
    assert.equal(again[0].content[0].type, 'input_image');
  }
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

// ---- scripted turns: a fake cased and a fake provider behind global fetch ----
async function fakeCased(routes) {
  const hits = [];
  const srv = http.createServer((req, res) => {
    hits.push(`${req.method} ${req.url}`);
    const hit = Object.entries(routes).find(([k]) => req.url.startsWith(k));
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify(hit ? hit[1] : {}));
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const was = process.env.CASE_URL;
  process.env.CASE_URL = `http://127.0.0.1:${srv.address().port}/v1`;
  return { hits, close: () => { process.env.CASE_URL = was; srv.close(); } };
}
/** One Messages SSE body per round: blocks are {text} or {tool, input}. */
function anthropicSse({ blocks, usage = {} }) {
  const ev = (type, data) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
  let out = ev('message_start', { message: { id: 'msg', type: 'message', role: 'assistant', model: 'm', content: [],
    stop_reason: null, usage: { input_tokens: 0, output_tokens: 1, ...usage } } });
  blocks.forEach((b, index) => {
    if (b.tool) {
      out += ev('content_block_start', { index, content_block: { type: 'tool_use', id: b.id, name: b.tool, input: {} } });
      out += ev('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: JSON.stringify(b.input || {}) } });
    } else {
      out += ev('content_block_start', { index, content_block: { type: 'text', text: '' } });
      out += ev('content_block_delta', { index, delta: { type: 'text_delta', text: b.text } });
    }
    out += ev('content_block_stop', { index });
  });
  const stop = blocks.some((b) => b.tool) ? 'tool_use' : 'end_turn';
  return out + ev('message_delta', { delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: 5 } })
    + ev('message_stop', {});
}
async function withAnthropic(rounds, fn) {
  const sent = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    sent.push(JSON.parse(init.body));
    return new Response(anthropicSse(rounds.shift()), { status: 200, headers: { 'content-type': 'text/event-stream' } });
  };
  try { return await fn(sent); } finally { globalThis.fetch = orig; }
}

// Anthropic turns are bounded by billed input, not raw: 50k cache reads bill as
// 5k. Past 80% of the budget the model gets BUDGET_WARN, and a re-snapshot of an
// unchanged page is elided — the same as the OpenAI loop.
{
  const cased = await fakeCased({
    '/v1/computers/c_1/page': { ok: true, url: 'https://x/', elements: ['[1] button "Go"'] },
    '/v1/computers/c_1': { name: 'desk', credentials: [] },
  });
  const thread = { id: 't_budget', title: 't', agent: '', items: [], created: 0, updated: 0 };
  const events = [];
  try {
    const result = await withAnthropic([
      { usage: { input_tokens: 100, cache_read_input_tokens: 50000 }, blocks: [{ tool: 'computer_snapshot', id: 'tu_1' }] },
      { usage: { input_tokens: 100, cache_read_input_tokens: 30000 }, blocks: [{ tool: 'computer_snapshot', id: 'tu_2' }] },
      { usage: { input_tokens: 100 }, blocks: [{ text: 'done' }] },
    ], async (sent) => {
      const r = await runTurn({
        thread, inputText: 'look', auth: { provider: 'anthropic', key: 'sk-ant' }, computerId: 'c_1',
        model: 'claude-sonnet-4-6', emit: (e) => events.push(e),
      });
      assert.equal(sent.length, 3, 'raw input is over budget after round 1; billed (5.1k) is not');
      const last = JSON.stringify(sent[2].messages.at(-1));
      assert.ok(last.includes('Turn budget nearly spent'), 'round 3 carries the wrap-up warning');
      assert.ok(last.includes('same elements as the previous snapshot'), 'the repeat snapshot is elided live');
      return r;
    });
    assert.equal(result.finished, true);
    assert.equal(result.text, 'done');
    assert.ok(events.some((e) => e.type === 'think' && /80% of the turn budget/.test(e.text)));
    const outs = thread.items.filter((it) => it.type === 'function_call_output');
    assert.match(outs[1].output, /"unchanged":true/, 'history matches what the model saw');
    assert.ok(!JSON.stringify(thread.items).includes('Turn budget nearly spent'), 'the warning is turn-scoped');
  } finally {
    cased.close();
  }
}

// A png inside a tool's JSON (handoff_get, click with screenshot:true) reaches the
// model as an image, not as clipped base64 text.
{
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
  const cased = await fakeCased({
    '/v1/handoffs/h_1': { id: 'h_1', status: 'pending', screenshot_png_b64: png },
    '/v1/computers/c_1': { name: 'desk', credentials: [] },
  });
  const thread = { id: 't_hshot', title: 't', agent: '', items: [], created: 0, updated: 0 };
  try {
    await withAnthropic([
      { blocks: [{ tool: 'handoff_get', id: 'tu_1', input: { handoff_id: 'h_1' } }] },
      { blocks: [{ text: 'seen' }] },
    ], async (sent) => {
      await runTurn({
        thread, inputText: 'check', auth: { provider: 'anthropic', key: 'sk-ant' }, computerId: 'c_1',
        model: 'claude-sonnet-4-6', emit: () => {},
      });
      const res = sent[1].messages.at(-1).content[0];
      assert.equal(res.type, 'tool_result');
      assert.ok(!res.content[0].text.includes(png), 'no base64 in the text');
      assert.match(res.content[0].text, /"status":"pending"/);
      assert.deepEqual(res.content[1], { type: 'image', source: { type: 'base64', media_type: 'image/png', data: png } });
    });
    assert.ok(thread.items.some((it) => it.shot), 'history keeps the png as a screenshot item');
    assert.ok(!JSON.stringify(thread.items).includes(png));
  } finally {
    cased.close();
  }
}

function fakeCodex(dir, { loggedIn = true } = {}) {
  const bin = path.join(dir, 'codex');
  const log = path.join(dir, 'argv.log');
  fs.writeFileSync(bin, `#!/bin/bash
if [ "$1" = "login" ] && [ "$2" = "status" ]; then exit ${loggedIn ? 0 : 1}; fi
if [ "$1" = "exec" ]; then
  echo "$@" >> ${log}
  echo '{"type":"thread.started","thread_id":"thr_1"}'
  echo '{"type":"item.started","item":{"id":"c1","type":"mcp_tool_call","tool":"computer_navigate","arguments":{"url":"https://example.com"}}}'
  echo '{"type":"item.completed","item":{"id":"c1","type":"mcp_tool_call","tool":"computer_navigate","status":"completed","result":{"ok":true}}}'
  echo '{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"Example Domain"}}'
  echo '{"type":"turn.completed","usage":{}}'
  exit 0
fi
exit 1
`, { mode: 0o755 });
  return { bin, log };
}

{
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'case-brain-'));
  const gone = fakeCodex(fs.mkdtempSync(path.join(os.tmpdir(), 'case-codexbin-')), { loggedIn: false });
  process.env.CASE_CODEX_BIN = gone.bin;
  const env = { CASE_DRIVE_API_KEY: 'sk-env-key', CASE_DRIVE_PROVIDER: 'openai' };
  assert.equal(brainAuth({}, home), null);
  putProviderKey('openai', 'sk-saved-openai-key', home);
  assert.deepEqual(brainAuth({}, home), { auth: { provider: 'openai', key: 'sk-saved-openai-key' }, model: 'gpt-6-luna' });
  await new Promise((r) => setTimeout(r, 5));
  putProviderKey('anthropic', 'sk-ant-saved-key', home);
  assert.deepEqual(brainAuth({ CASE_DRIVE_MODEL: 'claude-opus-5-5' }, home),
    { auth: { provider: 'anthropic', key: 'sk-ant-saved-key' }, model: 'claude-opus-5-5' });
  assert.deepEqual(brainAuth(env, home), { auth: { provider: 'openai', key: 'sk-env-key' }, model: 'gpt-6-luna' }, 'the .env key stays first');

  assert.deepEqual(chatBrainAuth({ 'x-openai-key': 'sk-tab' }, { model: 'claude-sonnet-5-5' }, home), { provider: 'openai', key: 'sk-tab' }, 'the tab key wins');
  assert.deepEqual(chatBrainAuth({}, { model: 'claude-sonnet-5-5' }, home), { provider: 'anthropic', key: 'sk-ant-saved-key' });
  assert.deepEqual(chatBrainAuth({}, { model: 'gpt-6-luna' }, home), { provider: 'openai', key: 'sk-saved-openai-key' });
  assert.deepEqual(chatBrainAuth({}, { provider: 'chatgpt' }, home), { provider: 'chatgpt', key: '' });

  const on = fakeCodex(fs.mkdtempSync(path.join(os.tmpdir(), 'case-codexbin-')));
  process.env.CASE_CODEX_BIN = on.bin;
  assert.deepEqual(chatBrainAuth({}, { provider: 'chatgpt' }, home), { provider: 'chatgpt', key: 'chatgpt' });
  const authFile = path.join(home, 'codex', 'auth.json');
  fs.mkdirSync(path.dirname(authFile), { recursive: true });
  fs.writeFileSync(authFile, '{}');
  fs.utimesSync(authFile, new Date(Date.now() + 60_000), new Date(Date.now() + 60_000));
  assert.deepEqual(brainAuth({}, home), { auth: { provider: 'chatgpt', key: 'chatgpt' }, model: 'codex' }, 'the newest login wins');
  assert.equal(brainAuth(env, home).auth.provider, 'openai');
  fs.utimesSync(authFile, new Date(0), new Date(0));
  assert.equal(brainAuth({}, home).auth.provider, 'anthropic');
  fs.rmSync(home, { recursive: true, force: true });

  const cased = await fakeCased({ '/v1/computers/c_1': { name: 'desk', credentials: [] } });
  const thread = { id: 't_gpt', title: 't', agent: '', items: [], created: 0, updated: 0 };
  const events = [];
  const turn = (inputText, attaches = []) => runTurn({
    thread, inputText, attaches, auth: { provider: 'chatgpt', key: 'chatgpt' }, computerId: 'c_1',
    model: 'codex', effort: 'high', emit: (e) => events.push(e),
  });
  try {
    const first = await turn('open example.com');
    assert.equal(first.text, 'Example Domain');
    assert.equal(thread.codex_thread, 'thr_1');
    assert.equal(thread.brain, 'chatgpt');
    assert.equal(thread.agent, 'c_1', 'the first tool call claims the computer');
    assert.deepEqual(events.map((e) => e.type), ['start', 'claim', 'tool', 'tool_result', 'text', 'done']);
    assert.deepEqual(thread.items.map((it) => it.type || it.role), ['user', 'function_call', 'function_call_output', 'message']);
    await turn('again');
    const argvLines = fs.readFileSync(on.log, 'utf8').trim().split('\n');
    assert.ok(!argvLines[0].includes(' resume '));
    assert.match(argvLines[1], /resume thr_1 again$/);
    events.length = 0;
    const refused = await turn('read it', [{ path: '/tmp/a.pdf', name: 'a.pdf', mime: 'application/pdf' }]);
    assert.match(refused.error, /image attachments only/);
    assert.equal(events.at(-1).type, 'error');
    assert.equal(fs.readFileSync(on.log, 'utf8').trim().split('\n').length, 2, 'codex never ran');
    await turn('look', [{ path: '/tmp/a.png', name: 'a.png', mime: 'image/png' }]);
    assert.match(fs.readFileSync(on.log, 'utf8'), / -i \/tmp\/a\.png /);
  } finally {
    cased.close();
    delete process.env.CASE_CODEX_BIN;
  }
}

{
  const serveSrc = fs.readFileSync(fileURLToPath(new URL('./serve.mjs', import.meta.url)), 'utf8');
  const tool = (n) => ALL_TOOLS.find((t) => t.name === n);
  const hw = tool('handoff_wait');
  assert.ok(hw, 'handoff_wait is offered to the model');
  assert.equal(hw.parameters.properties.max_wait_s.type, 'integer');
  assert.ok(!/Re-snapshot after any click/.test(tool('computer_snapshot').description),
    'snapshot text no longer tells the model to re-snapshot every click');
  assert.ok(tool('computer_tabs').parameters.properties.action.enum.includes('close_others'));
  assert.match(tool('computer_wait_for').description, /On timeout the result carries/);
  assert.match(serveSrc, /query in the URL when the site supports it/, 'search-via-URL rule in the developer prompt');
  assert.match(serveSrc, /computer_exec first, verify once/, 'shell-before-GUI rule in the developer prompt');
  assert.match(serveSrc, /close_others also closes tabs the user keeps open/, 'own tabs by id; close_others only on request');
  console.log('agent efficiency text ok');
}

console.log('web-ui serve: all checks pass');
