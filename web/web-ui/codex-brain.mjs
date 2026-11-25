import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { secretsHome } from './provider-secrets.mjs';

export const DEVICE_URL = 'https://auth.openai.com/codex/device';
const LOGIN_MS = 15 * 60 * 1000;
const DEFAULT_MCP_SCRIPT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../mcp/case_mcp.py');

export function codexHome(home) {
  return path.join(secretsHome(home), 'codex');
}

export function ensureCodexHome(home) {
  const dir = codexHome(home);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
  return dir;
}

export function authPath(home) {
  return path.join(codexHome(home), 'auth.json');
}

const CACHE_MS = 5000;
const cache = new Map();

function cached(key, compute) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.value;
  const value = compute();
  cache.set(key, { at: Date.now(), value });
  return value;
}

export function clearCodexCache() {
  cache.clear();
}

const PASS_ENV = ['PATH', 'HOME', 'LANG', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy'];

function codexEnv(home) {
  const env = { CODEX_HOME: codexHome(home) };
  for (const k of PASS_ENV) {
    if (process.env[k]) env[k] = process.env[k];
  }
  return env;
}

export function codexBin() {
  const env = String(process.env.CASE_CODEX_BIN || '').trim();
  if (env) return env;
  return cached('which', () => {
    try {
      return execFileSync('which', ['codex'], { encoding: 'utf8' }).trim();
    } catch {
      return '';
    }
  });
}

function loginAtPath(home) {
  return path.join(codexHome(home), 'login_at');
}

function loginAt(home) {
  try {
    return fs.readFileSync(loginAtPath(home), 'utf8').trim() || null;
  } catch {
    return null;
  }
}

export function codexStatus(home) {
  const bin = codexBin();
  const updated_at = loginAt(home);
  if (!bin) return { configured: false, updated_at };
  const configured = cached(`status:${bin}:${home}`, () => {
    try {
      execFileSync(bin, ['login', 'status'], {
        env: codexEnv(home),
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 8000,
      });
      return true;
    } catch {
      return false;
    }
  });
  return { configured, updated_at };
}

function tomlStr(s) {
  return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

export function writeCodexConfig(home, opts = {}) {
  const dir = ensureCodexHome(home);
  const py = opts.mcpPython || process.env.CASE_MCP_PY || 'python3';
  const script = opts.mcpScript || process.env.CASE_MCP_SCRIPT || DEFAULT_MCP_SCRIPT;
  const caseUrl = opts.caseUrl || process.env.CASE_URL || 'http://127.0.0.1:8787/v1';
  const token = String(opts.caseToken ?? process.env.CASE_TOKEN ?? '').trim();
  const body = [
    'approval_policy = "never"',
    'sandbox_mode = "read-only"',
    'web_search = "disabled"',
    '',
    '[features]',
    'shell_tool = false',
    '',
    '[mcp_servers.case]',
    `command = ${tomlStr(py)}`,
    `args = [${tomlStr(script)}]`,
    'required = true',
    'default_tools_approval_mode = "approve"',
    '',
    '[mcp_servers.case.env]',
    `CASE_URL = ${tomlStr(caseUrl)}`,
    ...(token ? [`CASE_TOKEN = ${tomlStr(token)}`] : []),
    '',
  ].join('\n');
  fs.writeFileSync(path.join(dir, 'config.toml'), body, { mode: 0o600 });
  return path.join(dir, 'config.toml');
}

export function workDir(home, cid, system) {
  const safe = String(cid || 'none').replace(/[^A-Za-z0-9._-]/g, '_') || 'none';
  const dir = path.join(codexHome(home), 'work', safe);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(dir, 'AGENTS.md'), String(system || ''), { mode: 0o600 });
  return dir;
}

export function codexArgv({ dir, prompt, resume, effort, images } = {}) {
  const argv = [
    'exec', '--json', '--skip-git-repo-check',
    '-C', dir,
    '-c', `model_reasoning_effort="${effort || 'medium'}"`,
  ];
  for (const img of images || []) {
    if (img) argv.push('-i', img);
  }
  if (resume) argv.push('resume', String(resume));
  if (prompt) argv.push('--', String(prompt));
  return argv;
}

function clip(v, n = 4000) {
  const s = typeof v === 'string' ? v : JSON.stringify(v ?? '');
  return s.length <= n ? s : s.slice(0, n);
}

function killTree(child) {
  if (!child || child.killed) {
    try { if (child && child.pid) process.kill(-child.pid, 'SIGKILL'); } catch {}
    return;
  }
  try { process.kill(-child.pid, 'SIGTERM'); } catch { try { child.kill('SIGTERM'); } catch {} }
  try { process.kill(-child.pid, 'SIGKILL'); } catch { try { child.kill('SIGKILL'); } catch {} }
}

export function startDeviceLogin(home, { onCode, signal } = {}) {
  return new Promise((resolve, reject) => {
    const bin = codexBin();
    if (!bin) return reject(new Error('codex not installed'));
    ensureCodexHome(home);
    const child = spawn(bin, ['login', '--device-auth'], {
      env: codexEnv(home),
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: true,
    });
    let buf = '';
    let sent = false;
    const fire = () => {
      if (sent) return;
      const plain = buf.replace(/\x1b\[[0-9;]*m/g, '');
      const url = (plain.match(/https:\/\/auth\.openai\.com\/codex\/device/) || [])[0];
      const code = (plain.match(/\b[A-Z0-9]{4}-[A-Z0-9]{5}\b/) || [])[0];
      if (url && code) {
        sent = true;
        if (typeof onCode === 'function') onCode({ url, code });
      }
    };
    const onData = (c) => { buf += c.toString(); fire(); };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(t);
      if (signal) signal.removeEventListener('abort', abort);
      fn(arg);
    };
    const t = setTimeout(() => {
      killTree(child);
      done(reject, new Error('login timed out'));
    }, LOGIN_MS);
    const abort = () => {
      killTree(child);
      done(reject, new Error('login aborted'));
    };
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, { once: true });
    }
    child.on('error', (err) => done(reject, err));
    child.on('close', (code) => {
      if (code === 0) {
        fs.writeFileSync(loginAtPath(home), new Date().toISOString(), { mode: 0o600 });
        clearCodexCache();
        done(resolve, { ok: true });
      }
      else done(reject, new Error('login failed'));
    });
  });
}

export function codexLogout(home) {
  const bin = codexBin();
  const dir = ensureCodexHome(home);
  if (bin) {
    try {
      execFileSync(bin, ['logout'], {
        env: codexEnv(home),
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 8000,
      });
    } catch {}
  }
  try { fs.unlinkSync(path.join(dir, 'auth.json')); } catch {}
  try { fs.unlinkSync(loginAtPath(home)); } catch {}
  clearCodexCache();
  return { configured: false, updated_at: null };
}

export function runCodexTurn({
  cid, prompt, resume, effort, images, home, emit, items, signal, system,
} = {}) {
  emit = typeof emit === 'function' ? emit : () => {};
  items = items || [];
  const bin = codexBin();
  if (!bin) return Promise.reject(new Error('codex not installed'));
  writeCodexConfig(home);
  const dir = workDir(home, cid, system);
  const argv = codexArgv({ dir, prompt, resume, effort, images });
  return new Promise((resolve, reject) => {
    const child = spawn(bin, argv, {
      env: codexEnv(home),
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: true,
    });
    let errored = false;
    let lineBuf = '';
    let thread_id = resume ? String(resume) : '';
    let text = '';
    let finished = false;
    let usage = null;
    let killed = false;
    const kill = (msg) => {
      if (killed) return;
      killed = true;
      if (msg) emit({ type: 'error', error: msg });
      killTree(child);
    };
    const abort = () => kill();
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, { once: true });
    }
    const handle = (ev) => {
      if (!ev || typeof ev !== 'object') return;
      if (ev.type === 'thread.started') {
        thread_id = ev.thread_id || ev.thread?.id || thread_id;
        return;
      }
      if (ev.type === 'turn.completed') {
        finished = true;
        usage = ev.usage || null;
        return;
      }
      if (ev.type === 'turn.failed' || ev.type === 'error') {
        errored = true;
        emit({ type: 'error', error: ev.error?.message || ev.message || 'codex failed' });
        return;
      }
      const item = ev.item;
      if (!item) return;
      const kind = item.type;
      if (ev.type === 'item.completed' && kind === 'reasoning') {
        emit({ type: 'think', text: item.text || '' });
        return;
      }
      if (ev.type === 'item.started' && kind === 'mcp_tool_call') {
        const id = item.id;
        const name = item.tool || item.name || 'tool';
        const args = item.arguments || {};
        emit({ type: 'tool', id, call_id: id, name, act: name, args });
        items.push({
          type: 'function_call',
          call_id: id,
          name,
          arguments: typeof args === 'string' ? args : JSON.stringify(args || {}),
        });
        return;
      }
      if (ev.type === 'item.completed' && kind === 'mcp_tool_call') {
        const id = item.id;
        const name = item.tool || item.name || 'tool';
        const ok = item.status === 'completed';
        const detail = clip(item.result || item.error || '');
        emit({ type: 'tool_result', id, call_id: id, ok, name, detail });
        items.push({ type: 'function_call_output', call_id: id, output: detail });
        return;
      }
      if (ev.type === 'item.completed' && kind === 'agent_message') {
        text = item.text || '';
        emit({ type: 'text', text });
        items.push({ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] });
        return;
      }
      if (kind === 'command_execution') {
        kill('ChatGPT brain tried to run a host shell');
      }
    };
    const onChunk = (c) => {
      lineBuf += c.toString();
      const parts = lineBuf.split('\n');
      lineBuf = parts.pop() || '';
      for (const line of parts) {
        const s = line.trim();
        if (!s) continue;
        try { handle(JSON.parse(s)); } catch {}
      }
    };
    child.stdout.on('data', onChunk);
    child.stderr.on('data', () => { });
    child.on('error', (err) => {
      if (signal) signal.removeEventListener('abort', abort);
      reject(err);
    });
    child.on('close', (code) => {
      if (signal) signal.removeEventListener('abort', abort);
      if (lineBuf.trim()) {
        try { handle(JSON.parse(lineBuf)); } catch {}
      }
      if (code !== 0 && !text && !finished && !killed && !errored) emit({ type: 'error', error: 'ChatGPT brain failed' });
      resolve({ thread_id, text, finished: finished || code === 0, usage, code });
    });
  });
}
