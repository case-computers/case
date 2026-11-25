#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  codexHome, ensureCodexHome, authPath, codexBin, codexStatus, writeCodexConfig,
  workDir, codexArgv, startDeviceLogin, runCodexTurn, codexLogout, clearCodexCache,
} from './codex-brain.mjs';

const SENTINEL = 'sk-sentinel-NEVER-RETURN-THIS';
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'case-codex-'));
const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'case-codex-bin-'));
const fake = path.join(binDir, 'codex');

fs.writeFileSync(fake, `#!/bin/bash
set -e
cmd="$1"
if [ "$cmd" = "login" ]; then
  if [ "$2" = "status" ]; then
    [ -f "$(dirname "$0")/status_fail" ] && exit 1; exit 0
  fi
  if [ "$2" = "--device-auth" ]; then
    printf 'Follow these steps to sign in with ChatGPT using device code authorization:\n'
    echo -e "\\033[94mhttps://auth.openai.com/codex/device\\033[0m"
    echo -e "\\033[94mABCD-EFGHI\\033[0m"
    if [ -f "$(dirname "$0")/slow" ]; then sleep 30; fi
    exit 0
  fi
fi
if [ "$cmd" = "logout" ]; then
  exit 0
fi
if [ "$cmd" = "exec" ]; then
  if [ -f "$(dirname "$0")/fail" ]; then echo boom-secret >&2; exit 2; fi
  if [ -f "$(dirname "$0")/envdump" ]; then env > "$(dirname "$0")/env.txt"; fi
  if [ -f "$(dirname "$0")/slow" ]; then sleep 30; exit 0; fi
  echo '{"type":"thread.started","thread_id":"thr_test1"}'
  echo '{"type":"item.completed","item":{"id":"r1","type":"reasoning","text":"thinking"}}'
  echo '{"type":"item.started","item":{"id":"c1","type":"mcp_tool_call","server":"case","tool":"computer_navigate","arguments":{"url":"https://example.com"},"status":"in_progress"}}'
  echo '{"type":"item.completed","item":{"id":"c1","type":"mcp_tool_call","server":"case","tool":"computer_navigate","arguments":{"url":"https://example.com"},"status":"completed","result":{"ok":true}}}'
  echo '{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"Example Domain"}}'
  echo '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}'
  exit 0
fi
exit 1
`, { mode: 0o755 });

process.env.CASE_CODEX_BIN = fake;
const flagFile = (n) => path.join(binDir, n);
const setFlag = (n, on) => (on ? fs.writeFileSync(flagFile(n), '') : fs.rmSync(flagFile(n), { force: true }));

assert.equal(codexBin(), fake);
assert.equal(codexHome(home), path.join(home, 'codex'));
ensureCodexHome(home);
assert.equal((fs.statSync(codexHome(home)).mode & 0o777), 0o700);

fs.writeFileSync(authPath(home), SENTINEL, { mode: 0o600 });
fs.writeFileSync(path.join(codexHome(home), 'login_at'), new Date().toISOString(), { mode: 0o600 });
const st = codexStatus(home);
assert.equal(st.configured, true);
assert.ok(st.updated_at);
assert.ok(!JSON.stringify(st).includes(SENTINEL), 'codexStatus must not read auth.json');

setFlag('status_fail', true);
clearCodexCache();
assert.equal(codexStatus(home).configured, false);
setFlag('status_fail', false);
clearCodexCache();

const cfg = writeCodexConfig(home, {
  mcpPython: '/opt/case/.venv/bin/python',
  mcpScript: '/opt/case/mcp/case_mcp.py',
  caseUrl: 'http://127.0.0.1:8787/v1',
});
const toml = fs.readFileSync(cfg, 'utf8');
assert.match(toml, /shell_tool = false/);
assert.match(toml, /sandbox_mode = "read-only"/);
assert.match(toml, /required = true/);
assert.match(toml, /default_tools_approval_mode = "approve"/);
assert.match(toml, /approval_policy = "never"/);
assert.match(toml, /web_search = "disabled"/);
assert.ok(!toml.includes(SENTINEL));
assert.doesNotMatch(toml, /CASE_TOKEN/);
assert.match(fs.readFileSync(writeCodexConfig(home, { caseToken: 'tok"1' }), 'utf8'), /CASE_TOKEN = "tok\\"1"/);
assert.equal(fs.statSync(cfg).mode & 0o777, 0o600);

const wd = workDir(home, 'c_ab/../x', 'You operate Case computer c_ab.');
assert.equal(path.basename(wd), 'c_ab_.._x');
assert.equal(path.dirname(wd), path.join(codexHome(home), 'work'));
assert.match(fs.readFileSync(path.join(wd, 'AGENTS.md'), 'utf8'), /c_ab/);

const argv = codexArgv({
  dir: wd, prompt: 'go', resume: 'thr_old', effort: 'high', images: ['/tmp/a.png'],
});
assert.deepEqual(argv.slice(0, 5), ['exec', '--json', '--skip-git-repo-check', '-C', wd]);
assert.ok(argv.includes('resume'));
assert.equal(argv[argv.indexOf('resume') + 1], 'thr_old');
assert.ok(argv.includes('-i'));
assert.ok(argv.includes('go'));
assert.match(argv.find((a) => String(a).includes('model_reasoning_effort')) || '', /high/);

const hostile = codexArgv({ dir: wd, prompt: '--dangerously-bypass-approvals-and-sandbox', resume: 'thr_old', images: ['/tmp/a.png'] });
assert.deepEqual(hostile.slice(-2), ['--', '--dangerously-bypass-approvals-and-sandbox']);
assert.ok(hostile.indexOf('resume') < hostile.indexOf('--'));
assert.ok(hostile.indexOf('-i') < hostile.indexOf('--'));

const seen = [];
const login = await startDeviceLogin(home, { onCode: (c) => seen.push(c) });
assert.equal(login.ok, true);
assert.equal(seen.length, 1);
assert.equal(seen[0].url, 'https://auth.openai.com/codex/device');
assert.equal(seen[0].code, 'ABCD-EFGHI');
assert.ok(!JSON.stringify(seen).includes(SENTINEL));

const ac = new AbortController();
setTimeout(() => ac.abort(), 40);
setFlag('slow', true);
let aborted = false;
try {
  await startDeviceLogin(home, { signal: ac.signal });
} catch (e) {
  aborted = /abort|fail/i.test(String(e && e.message));
}
assert.ok(aborted, 'abort kills device login');
setFlag('slow', false);
assert.ok(Date.parse(fs.readFileSync(path.join(codexHome(home), 'login_at'), 'utf8')));
assert.equal(fs.statSync(path.join(codexHome(home), 'login_at')).mode & 0o777, 0o600);

const events = [];
const items = [{ role: 'user', content: 'open example.com' }];
const out = await runCodexTurn({
  cid: 'c_ab', prompt: 'open example.com', home, emit: (e) => events.push(e), items,
});
assert.equal(out.thread_id, 'thr_test1');
assert.equal(out.text, 'Example Domain');
assert.equal(out.finished, true);
assert.deepEqual(events.map((e) => e.type), ['think', 'tool', 'tool_result', 'text']);
assert.equal(events[1].name, 'computer_navigate');
assert.equal(events[2].ok, true);
assert.equal(items.filter((i) => i.type === 'function_call').length, 1);
assert.equal(items.filter((i) => i.type === 'function_call_output').length, 1);
assert.equal(items.at(-1).content[0].text, 'Example Domain');
assert.ok(!JSON.stringify({ events, items, out }).includes(SENTINEL));

setFlag('fail', true);
const failEvents = [];
const failed = await runCodexTurn({ cid: 'c_ab', prompt: 'x', home, emit: (e) => failEvents.push(e), items: [] });
assert.equal(failed.finished, false);
assert.deepEqual(failEvents, [{ type: 'error', error: 'ChatGPT brain failed' }]);
setFlag('fail', false);

const dump = flagFile('env.txt');
setFlag('envdump', true);
process.env.CASE_DRIVE_API_KEY = 'leak-me';
process.env.CASE_TOKEN = 'leak-me-too';
await runCodexTurn({ cid: 'c_ab', prompt: 'x', home, emit() {}, items: [] });
const dumped = fs.readFileSync(dump, 'utf8');
assert.match(dumped, new RegExp('^CODEX_HOME=' + codexHome(home), 'm'));
assert.doesNotMatch(dumped, /leak-me/);
setFlag('envdump', false);
delete process.env.CASE_DRIVE_API_KEY;
delete process.env.CASE_TOKEN;

setFlag('slow', true);
const killAc = new AbortController();
setTimeout(() => killAc.abort(), 40);
const slow = await runCodexTurn({
  cid: 'c_ab', prompt: 'slow', home, emit() {}, items: [], signal: killAc.signal,
});
assert.ok(slow.code !== 0 || killAc.signal.aborted, 'abort kills exec');

setFlag('slow', false);
fs.writeFileSync(authPath(home), SENTINEL);
assert.deepEqual(codexLogout(home), { configured: false, updated_at: null });
assert.equal(fs.existsSync(authPath(home)), false);
assert.equal(fs.existsSync(path.join(codexHome(home), 'login_at')), false);
delete process.env.CASE_CODEX_BIN;
fs.rmSync(home, { recursive: true, force: true });
fs.rmSync(binDir, { recursive: true, force: true });
console.log('codex-brain: all checks pass');
