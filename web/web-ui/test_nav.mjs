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

if (failed) {
  console.error(`\n${failed} failed`);
  process.exit(1);
}
console.log('\nnav states OK');
