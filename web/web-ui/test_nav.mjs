// SPDX-License-Identifier: MIT
/**
 * Sidebar states, without a browser.
 *
 * Drive is one seat at one desk, so the nav must show exactly the threads of the
 * picked computer and must never quietly seat you somewhere else (a different
 * computer is a different set of logins). Pulls navHtml/myThreads straight out of
 * index.html and runs them against fake state.
 *
 * Run: node web/web-ui/test_nav.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const page = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
const script = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join('\n');

/** Lift one top-level function out of the inline script by brace matching. */
function grab(name) {
  const start = script.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('index.html no longer defines ' + name);
  let depth = 0;
  for (let i = script.indexOf('{', start); i < script.length; i++) {
    if (script[i] === '{') depth++;
    else if (script[i] === '}' && --depth === 0) return script.slice(start, i + 1);
  }
  throw new Error('unbalanced braces in ' + name);
}

const mod = path.join(os.tmpdir(), 'case-nav-under-test.mjs');
fs.writeFileSync(mod, `
  let apiUp = true, comps = [], threads = [], comp = null, pickLost = false, activeTid = '';
  const esc = (s) => String(s ?? '');
  const fmtAge = () => '1h';
  ${grab('threadRowHtml')}
  ${grab('myThreads')}
  ${grab('navHtml')}
  ${grab('steerTarget')}
  ${grab('queuedThread')}
  // drainQ against a fake page: opening a thread is recorded, not loaded
  let chatCtl = null, loadingTid = '';
  const promptQ = [], calls = [];
  const paintQ = () => {};
  const newTask = () => { calls.push('new'); activeTid = ''; };
  const sendPrompt = (n) => calls.push('send ' + n.text + ' to ' + (activeTid || 'new'));
  const openThread = (t) => { calls.push('open ' + t); activeTid = loadingTid = t; };
  ${grab('drainQ')}
  ${grab('orphanQueued')}
  // the real openThread, against a fetch that fails
  let threadGen = 0, fetchFails = false, failStatus = 404;
  const inner = { innerHTML: '', appendChild() {} }, log = {}, md = (x) => x, paint = () => {};
  const fetch = async () => { if (fetchFails) throw new Error('down'); return { ok: false, status: failStatus, json: async () => ({ error: 'gone' }) }; };
  async ${grab('openThread').replace('function openThread(', 'function realOpenThread(')}
  export const fake = { promptQ, calls, drainQ, view: (t) => { activeTid = t; loadingTid = ''; },
                        loading: (t) => { activeTid = loadingTid = t; },
                        open: realOpenThread, state: () => ({ activeTid, loadingTid }),
                        down: (v) => { fetchFails = v; }, status: (v) => { failStatus = v; } };
  export const set = (s) => {
    apiUp = s.apiUp ?? true;
    comps = s.comps || [];
    threads = s.threads || [];
    comp = s.comp || null;
    pickLost = !!s.pickLost;
  };
  export { navHtml, myThreads, steerTarget, queuedThread };
`);

const nav = await import('file://' + mod);
fs.rmSync(mod, { force: true });

let failed = 0;
function assert(cond, msg) {
  if (cond) { console.log('ok  ' + msg); return; }
  console.error('FAIL ' + msg);
  failed++;
}

const A = { id: 'c_a', name: 'desk' };
const B = { id: 'c_b', name: 'other' };

nav.set({ apiUp: false });
assert(nav.navHtml().includes('cased unreachable'), 'cased down says so');

nav.set({ comps: [] });
assert(nav.navHtml().includes('No computers'), 'no computers points at deploy');

nav.set({ comps: [A], pickLost: true });
assert(nav.navHtml().includes('gone'), 'deleted pick says gone instead of falling back');

nav.set({ comps: [A], comp: null });
assert(nav.navHtml().includes('Pick a computer'), 'no pick asks for one');

nav.set({ comps: [A], comp: A, threads: [] });
assert(nav.navHtml().includes('No tasks yet'), 'picked but idle');

nav.set({ comps: [A, B], comp: A, threads: [
  { id: 't1', title: 'mine', agent: 'c_a', updated: 0 },
  { id: 't2', title: 'theirs', agent: 'c_b', updated: 0 },
  { id: 't3', title: 'legacy', agent: '', updated: 0 },
] });
const html = nav.navHtml();
assert(html.includes('mine'), 'shows this computer\'s threads');
assert(!html.includes('theirs'), 'hides another computer\'s threads');
assert(html.includes('legacy'), 'adopts pre-single-seat threads that have no agent');
assert(!html.includes('data-id='), 'sidebar lists no computers, only threads');

// A message typed mid-turn steers only the turn on screen; typed anywhere else it
// is queued for the thread it was typed into ('' is a new task).
assert(nav.steerTarget('t1', 't1') === 't1', 'typing into the running thread steers it');
assert(nav.steerTarget('t1', '') === '', 'typing into a new task does not steer the running turn');
assert(nav.steerTarget('t1', 't2') === '', 'typing into another thread does not steer the running turn');
assert(nav.steerTarget('__pending', '__pending') === '', 'a thread not yet born cannot be steered');
assert(nav.queuedThread({ text: 'x', tid: '' }, 't1') === '', 'a prompt typed into a new task starts one');
assert(nav.queuedThread({ text: 'x', tid: 't2' }, 't1') === 't2', 'a prompt typed into another thread goes there');
assert(nav.queuedThread({ text: 'x', tid: '__pending' }, 't1') === 't1', 'typed while the thread was being born: the one that ran');
assert(nav.queuedThread('x', 't1') === 't1', 'a queue item with no thread belongs to the turn that ran');

// A queued prompt is sent only into the thread it was typed into: if another thread
// is picked while that one loads, it waits instead of landing in the new pick.
const { fake } = nav;
fake.view('t1');
fake.promptQ.push({ text: 'hi', files: [], tid: 't2' });
fake.drainQ('t1');
assert(fake.calls.join() === 'open t2', 'a prompt for another thread opens it first');
fake.view('t3');                                   // clicked away while t2 loaded
assert(fake.promptQ.length === 1, 'and does not send while that thread is not on screen');
fake.view('t2');                                   // t2 finished loading: openThread drains
fake.drainQ('t2');
assert(fake.calls.join() === 'open t2,send hi to t2', 'it goes to t2 once t2 is up');
fake.calls.length = 0;
fake.loading('t5');                                // a turn ends while t5 is still loading
fake.promptQ.push({ text: 'later', files: [], tid: 't5' });
fake.drainQ('t4');
assert(fake.calls.length === 0 && fake.promptQ.length === 1,
  'a prompt for a thread still loading waits for the load');
fake.view('t5');
fake.drainQ('t5');
assert(fake.calls.join() === 'send later to t5', 'and is sent once it has loaded');
fake.calls.length = 0;
fake.promptQ.length = 0;
// A thread that fails to load is left unopened, so clicking it or draining retries.
for (const down of [false, true]) {
  fake.view('t1');
  fake.down(down);
  await fake.open('t6');
  const st = fake.state();
  assert(st.activeTid === '' && st.loadingTid === '',
    (down ? 'an unreachable server' : 'a thread gone') + ' leaves no thread open to send into');
}
fake.promptQ.push({ text: 'retry', files: [], tid: 't6' });
fake.drainQ('');
assert(fake.calls.join() === 'open t6' && fake.promptQ.length === 1,
  'a queued prompt for it retries the load instead of sending');
fake.calls.length = 0;
fake.promptQ.length = 0;
// A thread that is gone for good hands its queued prompts to a new task, and the
// prompts behind them are no longer stuck.
fake.view('t1');
fake.down(false);
fake.promptQ.push({ text: 'orphan', files: [], tid: 't7' }, { text: 'next', files: [], tid: 't1' });
await fake.open('t7');
assert(fake.calls.join() === 'send orphan to new',
  'a prompt for a deleted thread starts a new task instead of blocking the queue');
assert(fake.promptQ.length === 1 && fake.promptQ[0].text === 'next', 'and the rest stay queued in order');
fake.calls.length = 0;
fake.promptQ.length = 0;
fake.view('t1');
fake.status(401);                                  // the thread exists; this tab just lost its token
fake.promptQ.push({ text: 'keep', files: [], tid: 't8' });
await fake.open('t8');
assert(fake.calls.length === 0 && fake.promptQ[0].tid === 't8',
  'an unauthorized load keeps the prompt queued for its own thread');
fake.status(404);
fake.calls.length = 0;
fake.promptQ.length = 0;
fake.view('t2');
fake.promptQ.push({ text: 'fresh', files: [], tid: '' });
fake.drainQ('t2');
assert(fake.calls.join() === 'new,send fresh to new', 'a prompt typed into a new task starts one');

if (failed) {
  console.error(`\n${failed} failed`);
  process.exit(1);
}
console.log('\nnav states OK');
