#!/usr/bin/env node
// SPDX-License-Identifier: MIT
/**
 * Drive UI + deployer — talks to local cased (compose or CASE_URL).
 * Serves Drive at / (index.html) and the computer deployer at /deploy.
 *
 * CASE_LOCAL (default on): 127.0.0.1 / compose `cased` — no SSH tunnel, /live
 * relays noVNC through cased. CASE_LOCAL=0 is a no-op here (this process never
 * tunnels); it only flips the health `local` flag.
 *
 * OpenAI key arrives per-request in x-openai-key; Anthropic in x-anthropic-key;
 * never logged.
 * Optional CASE_TOKEN: Bearer, case_token cookie, or ?token= on first hit.
 *
 * Run: CASE_LOCAL=1 node web/web-ui/serve.mjs  →  http://127.0.0.1:4174/
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import OpenAI from 'openai';
import { CASE_TOOLS, caseCall, caseToolPlan, runCaseTool, streamEventToNdjson, tracesFromOutput, chatAuth, envDriveAuth, resolveChatModel, histToAnthropicMessages, anthropicToolLoop, withRateRetry } from './case-tools.mjs';
import * as ntfy from './ntfy.mjs';
import { PHONE_THREAD_ID, routePhone } from './phone.mjs';
import * as telegram from './telegram.mjs';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 4174);
const BIND = process.env.CASE_BIND || '127.0.0.1';
const TOKEN = (process.env.CASE_TOKEN || '').trim();
const HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', 'ui',
  ...(process.env.CASE_ALLOWED_HOSTS || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean),
  ...((process.env.CASE_PUBLIC_HOST || '').trim() ? [process.env.CASE_PUBLIC_HOST.trim().toLowerCase()] : [])]);
export function hostOf(v) {
  v = String(v || '').toLowerCase();
  return v.startsWith('[') ? v.slice(0, v.indexOf(']') + 1) : v.split(':')[0];
}
// Host must be ours, and so must a present Origin: the two together stop DNS
// rebinding, cross-site form posts and cross-site websocket opens.
export function browserOk(req, hosts = HOSTS) {
  if (!hosts.has(hostOf(req.headers.host))) return false;
  const o = req.headers.origin;
  if (!o) return true;
  try { return hosts.has(hostOf(new URL(o).host)); } catch { return false; }
}

export function parseCaseUrl(raw) {
  const u = new URL(String(raw || 'http://127.0.0.1:8787'));
  const port = u.port ? Number(u.port) : (u.protocol === 'https:' ? 443 : 80);
  return { hostname: u.hostname, port, protocol: u.protocol };
}
const CASE = parseCaseUrl(process.env.CASE_URL || 'http://127.0.0.1:8787');
process.env.CASE_URL = `${CASE.protocol}//${CASE.hostname}:${CASE.port}/v1`;

export function isLocalMode(env = process.env, host = CASE.hostname) {
  const flag = String(env.CASE_LOCAL || '').trim().toLowerCase();
  if (flag === '0' || flag === 'false') return false;
  if (flag === '1' || flag === 'true') return true;
  return host === '127.0.0.1' || host === 'localhost' || host === 'cased';
}
const LOCAL = isLocalMode();
const HOME = process.env.CASE_HOME || path.join(process.env.HOME || '/tmp', '.case');

export function liveCid(pathname) {
  const m = String(pathname || '').match(/^\/live\/(c_[A-Za-z0-9]+)(?=\/|$)/);
  return m ? m[1] : '';
}
export function liveDestPath(rawUrl) {
  const u = new URL(rawUrl || '/', 'http://x');
  const cid = liveCid(u.pathname);
  const rest = cid
    ? (u.pathname.slice(('/live/' + cid).length) || '/')
    : (u.pathname.replace(/^\/live/, '') || '/');
  return rest + u.search;
}
export function livePathHasDotDot(rawUrl) {
  const pathOnly = String(rawUrl || '').split('?')[0];
  if (pathOnly.includes('..')) return true;
  try {
    if (decodeURIComponent(pathOnly).includes('..')) return true;
  } catch {
    return true; // malformed percent-encoding: refuse
  }
  return false;
}
export function tokenMatches(req, need = TOKEN) {
  if (!need) return true;
  const auth = req.headers.authorization || '';
  let got = '';
  if (auth.toLowerCase().startsWith('bearer ')) got = auth.slice(7).trim();
  if (!got) {
    for (const part of String(req.headers.cookie || '').split(';')) {
      const [k, ...rest] = part.trim().split('=');
      if (k === 'case_token') { got = rest.join('='); break; }
    }
  }
  if (!got) {
    try { got = new URL(req.url || '/', 'http://x').searchParams.get('token') || ''; }
    catch { /* ignore */ }
  }
  if (!got || got.length !== need.length) return false;
  return crypto.timingSafeEqual(Buffer.from(got), Buffer.from(need));
}

// ---------- small helpers (exported for tests) ----------
export function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

export function pathOk(p) {
  const s = String(p || '');
  return s.startsWith('/') && !s.includes('\0') && !/(^|\/)\.\.(\/|$)/.test(s);
}

/** Parse `find -mindepth 1 -maxdepth 1 -printf '%y\t%s\t%T@\t%f\n'` output. */
export function parseFind(text) {
  const out = [];
  for (const line of String(text || '').split('\n')) {
    if (!line) continue;
    const [y, size, mtime, ...rest] = line.split('\t');
    const name = rest.join('\t');
    if (!name || name === '.' || name === '..') continue;
    out.push({
      name,
      dir: y === 'd',
      link: y === 'l',
      size: Number(size) || 0,
      mtime: Math.floor(Number(mtime) || 0),
    });
  }
  out.sort((a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name));
  return out;
}

const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.md': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
  '.py': 'text/plain; charset=utf-8', '.sh': 'text/plain; charset=utf-8',
  '.yml': 'text/plain; charset=utf-8', '.yaml': 'text/plain; charset=utf-8',
  '.toml': 'text/plain; charset=utf-8', '.csv': 'text/plain; charset=utf-8',
  '.log': 'text/plain; charset=utf-8', '.xml': 'text/xml; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.gif': 'image/gif',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  '.pdf': 'application/pdf',
};
export function mimeFor(p) {
  return MIME[path.extname(String(p || '')).toLowerCase()] || 'application/octet-stream';
}

// ---------- cased ----------
// One HTTP client (case-tools.caseCall); default timeout here stays 30s.
const api = (method, rel, opts = {}) => caseCall(method, rel, { timeoutMs: 30000, ...opts });

function originHealth() {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: CASE.hostname, port: CASE.port, method: 'GET', path: '/health',
      headers: { accept: 'application/json', ...(TOKEN ? { authorization: 'Bearer ' + TOKEN } : {}) },
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        let json = null;
        try { json = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { /* not json */ }
        resolve({ status: res.statusCode || 0, json });
      });
    });
    req.setTimeout(4000, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.end();
  });
}

let cachedCid = '';
async function cid() {
  if (cachedCid) return cachedCid;
  const r = await api('GET', '/computers', { timeoutMs: 6000 });
  cachedCid = r.json?.computers?.[0]?.id || '';
  return cachedCid;
}

function send(res, code, body, type = 'text/plain; charset=utf-8') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(body);
}
function json(res, code, obj) { send(res, code, JSON.stringify(obj), 'application/json; charset=utf-8'); }

const BODY_CAP = 1 << 20; // 1 MB, chat input is capped to 32k anyway
async function readBody(req, res, cap = BODY_CAP) {
  const chunks = [];
  let n = 0;
  for await (const c of req) {
    n += c.length;
    if (n > cap) { json(res, 413, { error: 'body too large' }); req.destroy(); return null; }
    chunks.push(c);
  }
  return Buffer.concat(chunks);
}

// ---------- routes ----------
async function computers(res) {
  try {
    const [r, h] = await Promise.all([
      api('GET', '/computers', { timeoutMs: 6000 }),
      originHealth().catch(() => ({ json: null })),
    ]);
    if (r.status >= 400 || !r.json) return json(res, 502, { error: r.json?.error?.message || 'cased unreachable', up: false, local: LOCAL });
    const rows = (r.json.computers || []).map((c) => ({
      id: c.id, name: c.name, state: c.state === 'running' ? 'awake' : c.state,
      credentials: c.credentials || [], pending_handoffs: c.pending_handoffs || 0,
      cpus: c.resources?.cpus ?? null, ram_mb: c.resources?.ram_mb ?? null,
    }));
    const pick = rows.find((c) => c.state === 'awake') || rows[0];
    if (pick) cachedCid = pick.id;
    const awake = ['awake', 'running', 'waking', 'creating'];
    const running = rows.filter((c) => awake.includes(c.state)).length;
    const maxRunning = Number(h.json?.max_running) || 0;
    return json(res, 200, {
      computers: rows, live: CASE.hostname, up: true, local: LOCAL,
      max_running: maxRunning, running: Number(h.json?.running) || running,
      max_ram_mb: Number(h.json?.max_ram_mb) || 0, ram_mb: Number(h.json?.ram_mb) || 0,
    });
  } catch {
    return json(res, 502, { error: 'cased unreachable', up: false, local: LOCAL });
  }
}

async function createComputer(res, req) {
  const buf = await readBody(req, res);
  if (!buf) return;
  let name = 'desk';
  const body = {};
  try {
    const b = JSON.parse(buf.toString('utf8') || '{}');
    if (b.name) name = String(b.name).slice(0, 40);
    // Pass sizing through untouched; cased validates the range and owns the 400.
    if (b.cpus != null) body.cpus = b.cpus;
    if (b.ram_mb != null) body.ram_mb = b.ram_mb;
  } catch { /* default name */ }
  try {
    const r = await api('POST', '/computers', { body: { ...body, name }, timeoutMs: 90000 });
    if (r.json?.id) cachedCid = r.json.id;
    return json(res, r.status >= 400 ? r.status : 201, r.json || { error: 'create failed' });
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

async function fsList(res, url) {
  const p = url.searchParams.get('path') || '/home/agent';
  if (!pathOk(p)) return json(res, 400, { error: 'bad path' });
  try {
    const id = await cid();
    if (!id) return json(res, 409, { error: 'no computer on the box' });
    const cmd = `find ${shq(p)} -mindepth 1 -maxdepth 1 -printf '%y\\t%s\\t%T@\\t%f\\n' 2>&1 || true`;
    const r = await api('POST', `/computers/${encodeURIComponent(id)}/exec?wake=true`,
      { body: { command: cmd, timeout_s: 15 }, timeoutMs: 30000 });
    if (r.status >= 400) return json(res, r.status, { error: r.json?.error?.message || 'exec failed' });
    const outText = r.json?.stdout || '';
    if (/No such file or directory/.test(outText) && !outText.includes('\t')) {
      return json(res, 404, { error: 'no such directory', path: p });
    }
    return json(res, 200, { path: p, entries: parseFind(outText) });
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

const FILE_CAP = 8 * 1024 * 1024;
async function fsFile(res, url) {
  const p = url.searchParams.get('path') || '';
  if (!pathOk(p)) return json(res, 400, { error: 'bad path' });
  try {
    const id = await cid();
    if (!id) return json(res, 409, { error: 'no computer on the box' });
    const r = await api('GET',
      `/computers/${encodeURIComponent(id)}/files?path=${encodeURIComponent(p)}&wake=true`,
      { timeoutMs: 60000, raw: true });
    if (r.status >= 400) {
      let msg = 'read failed';
      try { msg = JSON.parse(r.buf.toString('utf8'))?.error?.message || msg; } catch { /* keep */ }
      return json(res, r.status, { error: msg });
    }
    if (r.buf.length > FILE_CAP) return json(res, 413, { error: 'file over 8MB' });
    const ext = path.extname(p).toLowerCase();
    const inline = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'].includes(ext);
    res.writeHead(200, {
      'content-type': inline ? mimeFor(p) : 'application/octet-stream',
      'content-disposition': inline ? 'inline' : `attachment; filename="${path.basename(p).replace(/["\r\n]/g, '')}"`,
      'x-content-type-options': 'nosniff', 'cache-control': 'no-store',
    });
    return res.end(r.buf);
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

// ---------- credentials (vault via cased; same fields as /fill) ----------
export function normHost(s) {
  const h = String(s || '').trim().toLowerCase()
    .replace(/^[a-z]+:\/\//, '').split(/[/?#]/)[0].split('@').pop().split(':')[0];
  return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(h) ? h : '';
}
async function creds(req, res, url) {
  let id = '';
  try { id = await cid(); } catch { /* fall through */ }
  if (!id) return json(res, 502, { error: 'no computer — create one first' });
  const base = `/computers/${encodeURIComponent(id)}/credentials`;
  try {
    if (req.method === 'GET') {
      const r = await api('GET', base, { timeoutMs: 10000 });
      return json(res, r.status >= 400 ? r.status : 200, r.json || {});
    }
    if (req.method === 'DELETE') {
      const name = url.searchParams.get('name') || '';
      if (!name) return json(res, 400, { error: 'missing name' });
      const r = await api('DELETE', `${base}/${encodeURIComponent(name)}`, { timeoutMs: 10000 });
      if (r.status === 204) return json(res, 200, { ok: true });
      return json(res, r.status, r.json || { error: 'delete failed' });
    }
    if (req.method !== 'POST') return json(res, 405, { error: 'method' });
    const buf = await readBody(req, res);
    if (!buf) return;
    let b;
    try { b = JSON.parse(buf.toString('utf8') || '{}'); }
    catch { return json(res, 400, { error: 'bad json' }); }
    const domains = String(b.domains || '').split(/[\s,]+/).map(normHost).filter(Boolean);
    const username = String(b.username || '').trim();
    const secret = String(b.secret || '');
    if (!domains.length) return json(res, 400, { error: 'need a website like mail.google.com' });
    if (!username || !secret) return json(res, 400, { error: 'username and password required' });
    const body = { name: domains[0], username, secret, domains };
    const totp = String(b.totp_seed || '').replace(/\s+/g, '');
    if (totp) body.totp_seed = totp;
    const r = await api('POST', base, { body, timeoutMs: 10000 });
    return json(res, r.status >= 400 ? r.status : 200, r.json || {});
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

async function deleteComputer(res, req) {
  const buf = await readBody(req, res);
  if (!buf) return;
  let body = {};
  try { body = JSON.parse(buf.toString('utf8') || '{}'); } catch { /* empty */ }
  const id = String(body.computer_id || '').trim();
  const name = String(body.name || '').trim();
  if (!id || !name) return json(res, 400, { error: 'need computer_id and name' });
  try {
    const r = await api('DELETE', `/computers/${encodeURIComponent(id)}`, { body: { name }, timeoutMs: 60000 });
    if (r.status === 204 || r.status === 200) {
      if (cachedCid === id) cachedCid = '';
      return json(res, 200, { ok: true });
    }
    return json(res, r.status >= 400 ? r.status : 502, r.json || { error: 'delete failed' });
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

async function power(res, req, action) {
  try {
    const buf = await readBody(req, res);
    if (!buf) return;
    let body = {};
    try { body = JSON.parse(buf.toString('utf8') || '{}'); } catch { /* empty */ }
    const id = String(body.computer_id || '').trim() || await cid();
    if (!id) return json(res, 409, { error: 'no computer' });
    const r = await api('POST', `/computers/${encodeURIComponent(id)}/${action}`, { timeoutMs: 60000 });
    return json(res, r.status >= 400 ? r.status : 200, r.json || {});
  } catch (err) {
    return json(res, 502, { error: err.message || 'cased unreachable' });
  }
}

// Chat: same NDJSON contract as web/serve.mjs, hands always local REST.
// Tool names + semantics match mcp/case_mcp.py (prod default surface; no schedules).
const EXTRA_TOOLS = [
  { type: 'function', name: 'computer_list', description: 'List all computers with state, resources and credential names. Reuse an existing computer — only computer_create for an identity that should stay separate.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { type: 'function', name: 'computer_create', description: 'Create a persistent computer (Linux desktop + Chromium). Blocks until running. Computers are durable: logins, cookies and files survive sleep. Check computer_list first.', parameters: { type: 'object', properties: { name: { type: 'string' } }, additionalProperties: false } },
  { type: 'function', name: 'computer_screenshot', description: 'Screenshot of the computer display (1280x800 by default). Wakes if asleep. Prefer computer_snapshot for anything inside a web page; use this for canvas, visual layout, and anything outside the browser window. marks=true draws numbered snapshot boxes on the PNG (does not change the live page).', parameters: { type: 'object', properties: { marks: { type: 'boolean' } }, additionalProperties: false } },
  { type: 'function', name: 'computer_snapshot', description: 'Numbered list of visible interactive elements on the active browser tab. PREFER over screenshots for finding what to click: returns lines like [12] button "Save changes" — pass the number to computer_click_element or computer_fill. Starred lines (*[n]) appeared since the last snapshot. You rarely need this twice: navigate, click and fill(submit) each return the fresh snapshot themselves.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { type: 'function', name: 'computer_click_element', description: 'Click element [ref] from the last computer_snapshot. Pass name (quoted text from the snapshot line) so a changed page is refused with a fresh snapshot instead of a wrong click. text, if given, is typed into the element after the click. The result carries `snapshot` and the first 2000 chars of page text — do NOT call computer_snapshot after this.', parameters: { type: 'object', properties: { ref: { type: 'number' }, name: { type: 'string' }, text: { type: 'string' }, screenshot: { type: 'boolean' } }, required: ['ref'], additionalProperties: false } },
  { type: 'function', name: 'computer_hover', description: 'Hover the OS pointer over snapshot [ref] without clicking. Use for menus that only appear on hover. Pass name so a changed page is refused.', parameters: { type: 'object', properties: { ref: { type: 'number' }, name: { type: 'string' } }, required: ['ref'], additionalProperties: false } },
  { type: 'function', name: 'computer_upload', description: 'Assign a file already on the computer (path under /home/agent/, max 5MB) to snapshot [ref] which must be input[type=file]. Write the file first with computer_file_put or exec — do not send bytes here.', parameters: { type: 'object', properties: { ref: { type: 'number' }, path: { type: 'string' }, name: { type: 'string' } }, required: ['ref', 'path'], additionalProperties: false } },
  { type: 'function', name: 'computer_fill', description: 'Fill a whole form in one call: fields=[{ref, value}] with refs from computer_snapshot. Never for passwords or OTP codes — vaulted computer_login owns those. submit=true submits the form at the end, and the result then carries `snapshot` of the page it landed on — no computer_snapshot needed after one.', parameters: { type: 'object', properties: { fields: { type: 'array', items: { type: 'object', properties: { ref: { type: 'number' }, value: {} }, required: ['ref', 'value'], additionalProperties: false } }, submit: { type: 'boolean' } }, required: ['fields'], additionalProperties: false } },
  { type: 'function', name: 'computer_wait_for', description: 'Block until the page is ready instead of polling with eval. Exactly one of: selector (CSS), text (in body innerText), network_idle=true. gone=true inverts selector/text (wait for spinner to disappear).', parameters: { type: 'object', properties: { selector: { type: 'string' }, text: { type: 'string' }, gone: { type: 'boolean' }, network_idle: { type: 'boolean' }, timeout_s: { type: 'number' } }, additionalProperties: false } },
  { type: 'function', name: 'computer_tabs', description: 'Browser tabs: action=list|activate|new|close. eval/snapshot/capture talk to the ACTIVE tab — if a click opened a new tab and the page stopped responding, list then activate the right one. activate/close need target_id from list; new needs an http(s) url.', parameters: { type: 'object', properties: { action: { type: 'string', enum: ['list', 'activate', 'new', 'close'] }, target_id: { type: 'string' }, url: { type: 'string' } }, required: ['action'], additionalProperties: false } },
  { type: 'function', name: 'computer_capture_start', description: 'Start capturing network response bodies in the active tab whose URL matches url_pattern (regex). Survives SPA nav; catches fetch and XHR. Then navigate/act and drain with computer_capture_read. e.g. url_pattern="SearchTimeline|/graphql".', parameters: { type: 'object', properties: { url_pattern: { type: 'string' } }, required: ['url_pattern'], additionalProperties: false } },
  { type: 'function', name: 'computer_capture_read', description: 'Drain captured network responses. Buffer clears on each read. stop=true also ends the capture. Response bodies only — never request bodies/headers.', parameters: { type: 'object', properties: { stop: { type: 'boolean' } }, additionalProperties: false } },
  { type: 'function', name: 'computer_login', description: 'Log into a site using a vaulted credential (name from the vault list in the developer message). Returns success, failed, or handoff_pending when a human is needed for 2FA/OTP/captcha. Prefer this for any login wall — never ask the user for a password, never type into password fields, never click captchas, never call handoff_request for them. credential is the vault NAME (e.g. x.com), not an email. CALL AT MOST ONCE PER LOGIN. On handoff_pending, immediately call auth_attempt_wait in the same turn.', parameters: { type: 'object', properties: { credential: { type: 'string' }, url: { type: 'string' }, idempotency_key: { type: 'string' } }, required: ['credential', 'url'], additionalProperties: false } },
  { type: 'function', name: 'auth_attempt_wait', description: 'Block until a computer_login attempt advances. Call immediately after computer_login returns handoff_pending. Do not re-call computer_login. Do not ask the user to nudge. Pass attempt_id and since_revision from the login result. On wait_status=timeout while still awaiting_human, call again with the returned revision.', parameters: { type: 'object', properties: { attempt_id: { type: 'string' }, since_revision: { type: 'number' }, max_wait_s: { type: 'number' } }, required: ['attempt_id'], additionalProperties: false } },
  { type: 'function', name: 'computer_file_get', description: 'Read a file from the computer. Text files return {encoding:"utf8", content} readable directly. Binary files return {encoding:"base64"} — do not read base64 yourself; process binary on the computer with computer_exec instead.', parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'], additionalProperties: false } },
  { type: 'function', name: 'computer_file_put', description: 'Write a file on the computer. content_b64 is the file bytes, base64-encoded.', parameters: { type: 'object', properties: { path: { type: 'string' }, content_b64: { type: 'string' } }, required: ['path', 'content_b64'], additionalProperties: false } },
  { type: 'function', name: 'computer_sleep', description: 'Hibernate the computer. Disk state (sessions, cookies, files) survives, and it wakes in seconds. Sleep when a task is done.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { type: 'function', name: 'handoff_request', description: 'Ask the human for help. kind: approval|question (code/text), device|captcha|passkey (live desk). Prefer computer_login for vaulted logins and 2FA/OTP — the platform creates those handoffs. Use kind=device when the human must act on the live desktop (QR, passkey) and login did not open a challenge. Never use this to check a computer_login journey — use auth_attempt_wait.', parameters: { type: 'object', properties: { prompt: { type: 'string' }, kind: { type: 'string', enum: ['approval', 'question', 'device', 'captcha', 'passkey'] } }, required: ['prompt'], additionalProperties: false } },
  { type: 'function', name: 'handoff_list', description: 'List pending handoffs. For login journeys use auth_attempt_wait, not this list and not a fresh computer_login.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { type: 'function', name: 'handoff_get', description: 'Fetch one handoff by id (includes screenshot). For login journeys prefer auth_attempt_wait. pending = waiting on the human; validating = human acted, platform is verifying.', parameters: { type: 'object', properties: { handoff_id: { type: 'string' } }, required: ['handoff_id'], additionalProperties: false } },
  { type: 'function', name: 'case_skill', description: 'Procedural memory on this computer. action=list: skills saved here (check BEFORE a multi-step task; if one matches, read and follow it). action=read: full SKILL.md for name. When FOLLOWING a skill, quoted names that came from the original demonstration are anchors and examples — substitute the current task\'s parameters (a different recipient means search for and click THAT person, not the example one) while keeping the step structure. action=save: write content to /home/agent/skills/<name>/SKILL.md — after finishing a task the user may repeat, PROPOSE saving it. Content: SKILL.md with name+description frontmatter, numbered natural-language steps in tool vocabulary with quoted element names as anchors, a checkpoint after each state change, a "Done means" section. GENERALIZE when saving: values typed and items picked in one run are PARAMETERS — name the skill after the task class (x-dm-send, not x-dm-harsh), declare parameters in the description, keep the run\'s values as worked examples only. Keep steps to the fewest tool calls that work: one snapshot per navigation or state change, never one before every click — refs stay valid until the page changes. Logins are ONE computer_login step by vault credential NAME — never write usernames, passwords, OTPs or tokens; save rejects secret-shaped content. New skills are drafts until a later run follows them.', parameters: { type: 'object', properties: { action: { type: 'string', enum: ['list', 'read', 'save'] }, name: { type: 'string' }, content: { type: 'string' } }, required: ['action'], additionalProperties: false } },
];
const ALL_TOOLS = [...CASE_TOOLS, ...EXTRA_TOOLS];

export function extraPlan(name, args, id) {
  const a = args || {};
  const p = `/computers/${encodeURIComponent(id)}`;
  if (name === 'computer_list') return { method: 'GET', rel: '/computers', act: 'list computers' };
  if (name === 'computer_create') return { method: 'POST', rel: '/computers', body: a.name ? { name: a.name } : {}, timeoutMs: 90000, act: `create ${a.name || 'desk'}` };
  if (name === 'computer_screenshot') return { method: 'GET', rel: `${p}/screenshot?wake=true${a.marks ? '&marks=true' : ''}`, screenshot: true, act: a.marks ? 'screenshot marks' : 'screenshot' };
  if (name === 'computer_snapshot') return { method: 'GET', rel: `${p}/page?wake=true`, act: 'snapshot' };
  if (name === 'computer_click_element') return { method: 'POST', rel: `${p}/click?wake=true`, body: a, act: `click [${a.ref}]${a.name ? ' ' + a.name : ''}` };
  if (name === 'computer_hover') return { method: 'POST', rel: `${p}/hover?wake=true`, body: a, act: `hover [${a.ref}]` };
  if (name === 'computer_upload') return { method: 'POST', rel: `${p}/upload?wake=true`, body: a, timeoutMs: 90000, act: `upload ${a.path || ''} → [${a.ref}]` };
  if (name === 'computer_fill') return { method: 'POST', rel: `${p}/fill?wake=true`, body: a, act: `fill ${Array.isArray(a.fields) ? a.fields.length : 0} fields` };
  if (name === 'computer_wait_for') return { method: 'POST', rel: `${p}/wait?wake=true`, body: a, timeoutMs: ((a.timeout_s || 30) + 20) * 1000, act: `wait ${a.selector || a.text || 'network idle'}` };
  if (name === 'computer_tabs') return { method: 'POST', rel: `${p}/tabs?wake=true`, body: a, act: `tabs ${a.action || 'list'}` };
  if (name === 'computer_capture_start') return { method: 'POST', rel: `${p}/capture?wake=true`, body: { pattern: a.url_pattern }, act: `capture ${a.url_pattern || ''}` };
  if (name === 'computer_capture_read') return { method: a.stop ? 'DELETE' : 'GET', rel: `${p}/capture?wake=true`, act: a.stop ? 'capture stop' : 'capture read' };
  if (name === 'computer_file_get') return { method: 'GET', rel: `${p}/files?path=${encodeURIComponent(a.path || '')}&wake=true`, rawFile: true, act: `read ${a.path || ''}` };
  if (name === 'computer_file_put') {
    return { method: 'PUT', rel: `${p}/files?path=${encodeURIComponent(a.path || '')}&wake=true`, rawPut: true, b64: a.content_b64, act: `write ${a.path || ''}` };
  }
  if (name === 'computer_login') {
    const body = { credential: a.credential, url: a.url };
    if (a.idempotency_key) body.idempotency_key = a.idempotency_key;
    return { method: 'POST', rel: `${p}/login?wake=true`, body, timeoutMs: 280000, act: `login ${a.credential || ''}` };
  }
  if (name === 'auth_attempt_wait') {
    const timeout = Math.max(1, Math.min(Number(a.max_wait_s) || 240, 240));
    const q = new URLSearchParams({
      timeout_s: String(timeout),
      after_revision: String(a.since_revision || 0),
    });
    return { method: 'GET', rel: `/auth-attempts/${encodeURIComponent(a.attempt_id || '')}/wait?${q}`, timeoutMs: (timeout + 15) * 1000, act: 'auth wait' };
  }
  if (name === 'computer_sleep') return { method: 'POST', rel: `${p}/sleep`, timeoutMs: 60000, act: 'sleep' };
  if (name === 'handoff_request') return { method: 'POST', rel: `${p}/handoffs`, body: { kind: a.kind || 'approval', prompt: a.prompt }, act: `handoff ${a.kind || 'approval'}` };
  if (name === 'handoff_list') return { method: 'GET', rel: '/handoffs?status=pending', act: 'handoffs' };
  if (name === 'handoff_get') return { method: 'GET', rel: `/handoffs/${encodeURIComponent(a.handoff_id || '')}`, act: 'handoff' };
  if (name === 'case_skill') return { skill: true, args: a, cid: id, act: `skill ${a.action}${a.name ? ' ' + a.name : ''}` };
  return null;
}

// Mirrors mcp/case_mcp.py case_skill: same index command, same secret screen.
const SKILL_DIR = '/home/agent/skills';
const SKILL_INDEX_CMD = `for f in ${SKILL_DIR}/*/SKILL.md; do [ -f "$f" ] || continue; echo "## $f"; awk '/^---$/{n++;next} n==1{print} n>=2{exit}' "$f"; done`;
const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;
const SKILL_RISKY_RE = /^\s*(?:password|passwd|secret|token|otp|totp[_-]?seed|api[_-]?key)\s*[:=]\s*\S+|[A-Za-z0-9_-]{40,}/im;
async function runSkill(plan) {
  const { args: a, cid: id, act } = plan;
  const p = `/computers/${encodeURIComponent(id)}`;
  try {
    if (a.action === 'list') {
      const r = await api('POST', `${p}/exec?wake=true`, { body: { command: SKILL_INDEX_CMD, timeout_s: 15 }, timeoutMs: 40000 });
      if (r.status >= 400) return { ok: false, error: r.json?.error || r.raw, act };
      return { ok: true, act, result: { skills: (r.json?.stdout || '').trim() || '(no skills saved on this computer yet)' } };
    }
    if (!SKILL_NAME_RE.test(a.name || '')) return { ok: false, error: 'bad skill name (lowercase slug)', act };
    const filePath = `${SKILL_DIR}/${a.name}/SKILL.md`;
    if (a.action === 'read') {
      const r = await api('GET', `${p}/files?path=${encodeURIComponent(filePath)}&wake=true`, { raw: true, timeoutMs: 30000 });
      if (r.status >= 400) return { ok: false, error: 'skill not found', act };
      return { ok: true, act, result: { name: a.name, content: r.buf.toString('utf8') } };
    }
    if (a.action === 'save') {
      if (!a.content || a.content.length > 65536) return { ok: false, error: 'content required, max 64KB', act };
      const risky = SKILL_RISKY_RE.exec(a.content);
      if (risky) return { ok: false, error: `content looks like it contains a secret (${JSON.stringify(risky[0].slice(0, 60))}) — reference vault credential NAMES only`, act };
      const put = await api('PUT', `${p}/files?path=${encodeURIComponent(filePath)}&wake=true`, { body: null, timeoutMs: 30000, rawBody: Buffer.from(a.content, 'utf8') });
      if (put.status >= 400) return { ok: false, error: put.json?.error || put.raw, act };
      const note = Buffer.from(`\n- skill \`${a.name}\` saved (${a.content.length} bytes)\n`).toString('base64');
      await api('POST', `${p}/exec?wake=true`, { body: { command: `mkdir -p /home/agent/reports && echo ${note} | base64 -d >> /home/agent/reports/$(date +%F).md`, timeout_s: 10 }, timeoutMs: 30000 });
      return { ok: true, act, result: { saved: filePath, status: 'draft until a run follows it' } };
    }
    return { ok: false, error: 'action must be list|read|save', act };
  } catch (err) {
    return { ok: false, error: err.message || 'cased unreachable', act };
  }
}

const FILE_TOOL_CAP = 256 * 1024;
async function runExtra(plan) {
  if (plan.skill) return runSkill(plan);
  try {
    if (plan.rawPut) {
      let buf;
      try { buf = Buffer.from(String(plan.b64 || ''), 'base64'); }
      catch { return { ok: false, error: 'bad base64', act: plan.act }; }
      const r = await api(plan.method, plan.rel, { raw: true, rawBody: buf, timeoutMs: 120000 });
      if (r.status >= 400) {
        let msg = 'write failed';
        try { msg = JSON.parse(r.buf.toString('utf8'))?.error?.message || msg; } catch { /* keep */ }
        return { ok: false, status: r.status, error: msg, act: plan.act };
      }
      let json = null;
      try { json = JSON.parse(r.buf.toString('utf8')); } catch { /* not json */ }
      return { ok: true, act: plan.act, result: json || { ok: true, bytes: buf.length } };
    }
    if (plan.screenshot) {
      const r = await api(plan.method, plan.rel, { raw: true, timeoutMs: 30000 });
      if (r.status >= 400) {
        let msg = 'screenshot failed';
        try { msg = JSON.parse(r.buf.toString('utf8'))?.error?.message || msg; } catch { /* keep */ }
        return { ok: false, status: r.status, error: msg, act: plan.act };
      }
      return {
        ok: true, act: plan.act,
        result: { format: 'png', bytes: r.buf.length },
        image_b64: r.buf.toString('base64'),
      };
    }
    if (plan.rawFile) {
      const r = await api(plan.method, plan.rel, { raw: true, timeoutMs: 60000 });
      if (r.status >= 400) {
        let msg = 'read failed';
        try { msg = JSON.parse(r.buf.toString('utf8'))?.error?.message || msg; } catch { /* keep */ }
        return { ok: false, status: r.status, error: msg, act: plan.act };
      }
      const buf = r.buf.slice(0, FILE_TOOL_CAP);
      const text = buf.toString('utf8');
      const utf8 = !text.includes('�');
      return {
        ok: true, act: plan.act,
        result: {
          encoding: utf8 ? 'utf8' : 'base64',
          content: utf8 ? text : buf.toString('base64'),
          bytes: r.buf.length,
          truncated: r.buf.length > FILE_TOOL_CAP || undefined,
        },
      };
    }
    const r = await api(plan.method, plan.rel, { body: plan.body, timeoutMs: plan.timeoutMs || 60000 });
    if (r.status >= 400) return { ok: false, status: r.status, error: r.json?.error || r.raw, act: plan.act };
    return { ok: r.json?.ok !== false, act: plan.act, result: r.json };
  } catch (err) {
    return { ok: false, error: err.message || 'cased unreachable', act: plan.act };
  }
}

function actFor(name, args, id) {
  const ep = extraPlan(name, args || {}, id);
  if (ep) return ep.act;
  return caseToolPlan(name, args || {}, id).act || name;
}

const ROUNDS = 200;
// ROUNDS bounds steps, not spend: history is re-sent every round, so cost is quadratic
// in rounds. This bounds the money — cumulative input tokens for one turn.
const TURN_TOKEN_BUDGET = Number(process.env.CASE_TURN_TOKENS || 2_000_000);
// Threads: the sidebar's unit of navigation, each with its own conversation memory.
// The Responses API runs stateless here (store:false), so the item list IS the
// memory. agent stays '' until the run first needs hands (a tool call executes) —
// that moment is the claim, and the UI animates the thread under its agent.
// Persisted beside this file so the navigator survives a restart.
const THREADS_FILE = process.env.CASE_THREADS || path.join(DIR, 'threads.json');
const THREADS = new Map();
try {
  for (const t of JSON.parse(fs.readFileSync(THREADS_FILE, 'utf8'))) {
    t.items = histCloseOpenCalls(migrateShots(t.items));
    THREADS.set(t.id, t);
  }
} catch { /* fresh */ }
const CHAT_BUSY = new Set();
const STEER = new Map(); // thread id -> user texts typed while the turn runs
function takeSteers(tid) {
  const q = STEER.get(tid) || [];
  if (q.length) STEER.delete(tid);
  return q;
}
function pushSteerItems(items, texts) {
  for (const n of texts) {
    items.push({ role: 'user', content: [{ type: 'input_text', text: n }] });
  }
}
function appendSteerToAnthropic(messages, text) {
  const last = messages[messages.length - 1];
  if (last?.role === 'user') {
    if (typeof last.content === 'string') last.content = last.content + '\n\n' + text;
    else if (Array.isArray(last.content)) last.content.push({ type: 'text', text });
    else messages.push({ role: 'user', content: text });
  } else {
    messages.push({ role: 'user', content: text });
  }
}
let saveTimer = null;
function saveThreads() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const MAX_THREADS = 200; // FIFO by updated; raise if users want deeper history
    if (THREADS.size > MAX_THREADS) {
      const stale = [...THREADS.values()].sort((a, b) => a.updated - b.updated)
        .slice(0, THREADS.size - MAX_THREADS);
      for (const t of stale) THREADS.delete(t.id);
    }
    try { fs.writeFileSync(THREADS_FILE, JSON.stringify([...THREADS.values()])); } catch { /* best-effort */ }
  }, 400);
}
if (THREADS.size) saveThreads();
export function newThread(title, agent) {
  const t = {
    id: 't_' + Math.random().toString(36).slice(2, 10),
    title: String(title).replace(/\s+/g, ' ').trim().slice(0, 72),
    agent: agent ? String(agent) : '', items: [], created: Date.now(), updated: Date.now(),
  };
  THREADS.set(t.id, t);
  return t;
}
const threadSummary = (t) => ({ id: t.id, title: t.title, agent: t.agent, updated: t.updated, created: t.created });
// Render view for reopening a thread: text + tool calls only, outputs and
// reasoning stay server-side.
export function threadTurns(items) {
  const turns = [];
  for (const it of items || []) {
    if (it.shot) continue; // screenshot attachments are model-only
    if (it.role === 'user') {
      const names = (it.attaches || []).map((a) => a.name).filter(Boolean);
      let text = '';
      if (Array.isArray(it.content)) {
        text = it.content.map((c) => (typeof c === 'string' ? c : c.text || c.input_text || '')).filter(Boolean).join('\n');
      } else if (it.content != null) {
        text = String(it.content);
      }
      const shown = [text, ...names].filter(Boolean).join('\n');
      if (!shown) continue; // screenshot attachments are model-only
      turns.push({ who: 'you', text: shown });
    }
    else if (it.type === 'function_call') turns.push({ who: 'tool', name: it.name, args: String(it.arguments || '').slice(0, 400) });
    else if (it.type === 'message' && it.role === 'assistant') turns.push({ who: 'agent', text: (it.content || []).map((c) => c.text || '').join('') });
  }
  return turns;
}
function threadsRoute(req, res, url) {
  const id = url.searchParams.get('id') || '';
  if (req.method === 'GET' && !id) {
    return json(res, 200, { threads: [...THREADS.values()].map(threadSummary).sort((a, b) => b.updated - a.updated) });
  }
  const t = THREADS.get(id);
  if (!t) return json(res, 404, { error: 'no such thread' });
  if (req.method === 'GET') return json(res, 200, { ...threadSummary(t), turns: threadTurns(t.items) });
  if (req.method === 'DELETE') { THREADS.delete(id); saveThreads(); return json(res, 200, { ok: true }); }
  return json(res, 405, { error: 'method' });
}
const HIST_MAX = 240_000;
export function histTrim(h, max = HIST_MAX) {
  // Turn boundaries are derived, not tracked: the only user-role items are the ones
  // that open a turn, so they survive any filtering of the array.
  let starts = h.items.map((it, i) => (it.role === 'user' ? i : -1)).filter((i) => i >= 0);
  while (starts.length > 1 && JSON.stringify(h.items).length > max) {
    const cut = starts[1];
    h.items.splice(0, cut);
    starts = starts.slice(1).map((i) => i - cut);
  }
}
/** Drop stale reasoning and close any function_call that has no output.
 *  OpenAI 400s "No tool output found for function call …" otherwise. */
export function histCloseOpenCalls(items, { keepReasoning = false } = {}) {
  const out = (items || []).filter((it) => it && (keepReasoning || it.type !== 'reasoning'));
  const have = new Set(out.filter((it) => it.type === 'function_call_output').map((it) => it.call_id));
  for (const it of [...out]) {
    if (it.type === 'function_call' && it.call_id && !have.has(it.call_id)) {
      out.push({ type: 'function_call_output', call_id: it.call_id, output: '{"ok":false,"error":"interrupted"}' });
      have.add(it.call_id);
    }
  }
  return out;
}
export function shotsDir(home = HOME) {
  return path.join(home, 'drive', 'shots');
}

/** Persist a screenshot to disk and return a tiny history item. The model still
 *  sees the image: hydrateShots re-reads the file when building the request. */
export function stashShot(b64, dir = shotsDir()) {
  const h = crypto.createHash('sha1').update(String(b64 || '')).digest('hex');
  const file = path.join(dir, h + '.png');
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* exists */ }
  try {
    if (!fs.existsSync(file)) fs.writeFileSync(file, Buffer.from(String(b64 || ''), 'base64'));
  } catch { /* best-effort; hydrateShots falls back to a text note */ }
  return { role: 'user', shot: file, content: [{ type: 'input_text', text: '[screenshot]' }] };
}

// A click that changed nothing yields a byte-identical png. Re-sending it buys
// no information and then rides along in every later round of the turn.
export function pushShot(items, shots, b64, dir = shotsDir()) {
  const h = crypto.createHash('sha1').update(b64).digest('hex');
  if (shots.has(h)) {
    items.push({ role: 'user', content: [{ type: 'input_text',
      text: 'screenshot identical to an earlier one this turn — the screen has not changed' }] });
    return;
  }
  shots.add(h);
  items.push(stashShot(b64, dir));
}

export function hydrateShots(items, dir = shotsDir()) {
  const root = path.resolve(dir) + path.sep;
  return (items || []).map((it) => {
    if (!it?.shot) return it;
    const abs = path.resolve(it.shot);
    if (!abs.startsWith(root) || path.extname(abs) !== '.png') {
      return { role: 'user', content: [{ type: 'input_text', text: '[screenshot]' }] };
    }
    try {
      const buf = fs.readFileSync(abs);
      return {
        role: 'user',
        content: [{
          type: 'input_image', detail: 'high',
          image_url: 'data:image/png;base64,' + buf.toString('base64'),
        }],
      };
    } catch {
      return { role: 'user', content: [{ type: 'input_text', text: '[screenshot]' }] };
    }
  });
}

export const ATTACH_MAX = 5 * 1024 * 1024;
export const ATTACH_MAX_N = 4;

export function inboxDir(home = HOME) {
  return path.join(home, 'drive', 'inbox');
}

export function attachKind(mime, name = '') {
  const m = String(mime || mimeFor(name) || '').split(';')[0].trim().toLowerCase();
  if (m === 'image/png' || m === 'image/jpeg' || m === 'image/gif' || m === 'image/webp') return 'image';
  if (m === 'application/pdf') return 'pdf';
  if (m.startsWith('text/') || m === 'application/json' || m === 'application/javascript'
    || m === 'text/javascript' || m === 'application/xml') return 'text';
  return '';
}

export function safeAttachName(name) {
  const base = path.basename(String(name || 'file')).replace(/[^\w.\-]+/g, '_').slice(0, 80);
  return base || 'file';
}

export function stashAttach(buf, name, mime, dir = inboxDir()) {
  const kind = attachKind(mime, name);
  if (!kind) {
    const err = new Error('file type not allowed');
    err.status = 400;
    throw err;
  }
  if (!buf || !buf.length) {
    const err = new Error('empty file');
    err.status = 400;
    throw err;
  }
  if (buf.length > ATTACH_MAX) {
    const err = new Error('file too large');
    err.status = 413;
    throw err;
  }
  const base = safeAttachName(name);
  const h = crypto.createHash('sha1').update(buf).digest('hex');
  const id = h + '-' + base;
  const file = path.join(dir, id);
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* exists */ }
  try {
    if (!fs.existsSync(file)) fs.writeFileSync(file, buf);
  } catch {
    const err = new Error('could not store file');
    err.status = 500;
    throw err;
  }
  if (!fs.existsSync(file)) {
    const err = new Error('could not store file');
    err.status = 500;
    throw err;
  }
  const stored = (mimeFor(base).split(';')[0] || mime || '').trim();
  return { id, name: base, mime: stored, path: file };
}

export function resolveAttach(id, dir = inboxDir()) {
  const raw = String(id || '');
  const base = path.basename(raw);
  if (!base || base !== raw || base === '.' || base === '..') return null;
  const root = path.resolve(dir) + path.sep;
  const abs = path.resolve(dir, base);
  if (!abs.startsWith(root) || !fs.existsSync(abs)) return null;
  const name = base.replace(/^[0-9a-f]{40}-/, '') || base;
  return { id: base, path: abs, name, mime: mimeFor(name).split(';')[0] };
}

function hydrateOneAttach(a, dir) {
  const name = a?.name || 'file';
  const note = { type: 'input_text', text: '[' + name + ']' };
  const abs = path.resolve(String(a?.path || ''));
  const root = path.resolve(dir) + path.sep;
  if (!abs.startsWith(root) || !fs.existsSync(abs)) return [note];
  const mime = String(a.mime || mimeFor(name)).split(';')[0].trim();
  const kind = attachKind(mime, name);
  try {
    const buf = fs.readFileSync(abs);
    if (kind === 'image') {
      return [{
        type: 'input_image', detail: 'high',
        image_url: 'data:' + mime + ';base64,' + buf.toString('base64'),
      }];
    }
    if (kind === 'pdf') {
      return [{
        type: 'input_file',
        filename: name,
        file_data: 'data:application/pdf;base64,' + buf.toString('base64'),
      }];
    }
    if (kind === 'text') {
      return [{ type: 'input_text', text: name + '\n' + clip(buf.toString('utf8'), 32000) }];
    }
  } catch { /* note */ }
  return [note];
}

export function hydrateAttaches(items, dir = inboxDir()) {
  return (items || []).map((it) => {
    if (!it?.attaches?.length) return it;
    const parts = [];
    if (typeof it.content === 'string' && it.content) {
      parts.push({ type: 'input_text', text: it.content });
    } else if (Array.isArray(it.content)) {
      for (const c of it.content) parts.push(c);
    }
    for (const a of it.attaches) parts.push(...hydrateOneAttach(a, dir));
    return { role: 'user', content: parts };
  });
}

async function attach(req, res) {
  const name = safeAttachName(req.headers['x-filename']);
  const mime = mimeFor(name);
  if (!attachKind(mime, name)) return json(res, 400, { error: 'file type not allowed' });
  const buf = await readBody(req, res, ATTACH_MAX);
  if (!buf) return;
  try {
    const rec = stashAttach(buf, name, mime);
    return json(res, 200, { id: rec.id, name: rec.name, mime: rec.mime });
  } catch (err) {
    return json(res, err.status || 400, { error: err.message || 'attach failed' });
  }
}

export function migrateShots(items, dir = shotsDir()) {
  let changed = false;
  const next = (items || []).map((it) => {
    if (it?.shot) return it;
    if (!(it?.role === 'user' && Array.isArray(it.content))) return it;
    const img = it.content.find((c) => c?.type === 'input_image' && typeof c.image_url === 'string');
    if (!img) return it;
    const m = /^data:image\/png;base64,(.+)$/.exec(img.image_url);
    if (!m) return it;
    changed = true;
    return stashShot(m[1], dir);
  });
  return changed ? next : items;
}

export function clip(v, n = 8000) {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  if (s.length <= n) return s;
  // Head+tail, not a tail-drop: a 150-element snapshot overruns n, and a blind cut
  // throws away the very fields that say so (count, truncated) along with the
  // closing brace, so the model gets mid-JSON garbage with no signal it was cut.
  const half = Math.floor((n - 40) / 2);
  return `${s.slice(0, half)}\n…${s.length - 2 * half} chars elided…\n${s.slice(-half)}`;
}

/** A re-snapshot after a click that changed nothing repeats the whole element list.
 *  Only the item being appended is replaced — never an earlier one, which would
 *  invalidate the cached prefix for the rest of the turn. */
export function snapshotElide(name, rest, snaps) {
  if (name !== 'computer_snapshot' || !rest.ok) return rest;
  const els = rest.result?.elements;
  if (!Array.isArray(els)) return rest;
  const h = crypto.createHash('sha1').update(els.join('\n')).digest('hex');
  if (snaps.last === h) {
    return { ok: true, act: rest.act, result: { unchanged: true, url: rest.result?.url,
      note: 'same elements as the previous snapshot — refs from it are still valid' } };
  }
  snaps.last = h;
  return rest;
}
/** Message typed while a turn runs. Queued here and appended to the running
 *  turn's history at the next round boundary — appending a user item lands
 *  after the cached prefix, so steering costs nothing in cache. */
async function steer(req, res) {
  const buf = await readBody(req, res);
  if (!buf) return;
  let body;
  try { body = JSON.parse(buf.toString('utf8') || '{}'); }
  catch { return send(res, 400, 'bad json'); }
  const tid = String(body.thread_id || '');
  const text = String(body.input || '').slice(0, 32000).trim();
  if (!text) return send(res, 400, 'empty input');
  if (!CHAT_BUSY.has(tid)) return json(res, 409, { error: 'no turn running — send as a normal message' });
  const q = STEER.get(tid) || [];
  q.push(text);
  STEER.set(tid, q);
  return json(res, 200, { queued: true });
}
export function phoneThread() {
  let t = THREADS.get(PHONE_THREAD_ID);
  if (t) return t;
  t = {
    id: PHONE_THREAD_ID, title: 'Phone', agent: '', items: [],
    created: Date.now(), updated: Date.now(),
  };
  THREADS.set(t.id, t);
  saveThreads();
  return t;
}

export async function runTurn({
  thread, inputText, attaches = [], auth, computerId = '',
  model, effort = 'medium', emit, stopped = () => false, signal, disconnect,
}) {
  thread.items = histCloseOpenCalls(thread.items);
  // Route, don't gate: the thread's own agent wins, then the client's pick, then
  // the box's computer. Assignment is only *claimed* when a tool actually runs.
  let id = thread.agent || computerId;
  if (!id) { try { id = await cid(); } catch { /* fall through */ } }
  if (!id) {
    const err = new Error('no computer — create one first');
    err.status = 502;
    throw err;
  }
  const goneP = disconnect || new Promise(() => {});
  const gone = signal ? { signal } : new AbortController();
  const toolOrStop = (start, act) => stopped()
    ? Promise.resolve({ ok: false, error: 'stopped by user', act })
    : Promise.race([start(), goneP.then(() => ({ ok: false, error: 'stopped by user', act }))]);
  emit({ type: 'start', computer_id: id, thread_id: thread.id, title: thread.title, agent: thread.agent, model, effort });
  let vault = '(none)';
  let cname = '';
  try {
    const cr = await api('GET', `/computers/${encodeURIComponent(id)}`, { timeoutMs: 6000 });
    const names = (cr.json?.credentials || []).map((c) => (typeof c === 'string' ? c : c.name)).filter(Boolean);
    if (names.length) vault = names.join(', ');
    cname = cr.json?.name || '';
  } catch { /* prompt still usable without the list */ }
  const claim = () => {
    if (thread.agent) return;
    thread.agent = id;
    emit({ type: 'claim', thread_id: thread.id, agent: id });
  };
  const dev = { role: 'developer', content: `You operate Case computer ${id}${cname ? ` (named "${cname}" — that's you when the user addresses it)` : ''} via tools. Loop: computer_navigate, then computer_click_element/computer_fill by ref. computer_hover for menus that only open on hover. computer_upload for input[type=file] (file already under /home/agent — write it first). If the snapshot has *[n] lines, those just appeared (autocomplete) — click one, do not press Enter. navigate, click and fill(submit) each RETURN the page's numbered elements and the first 2000 chars of page text, so read the result you already have — call computer_snapshot only for a first look or when something you did not do changed the page. Refs stay valid until the page changes. computer_wait_for instead of polling. Screenshots only for canvas/layout; marks=true draws numbered snapshot boxes on the PNG. Coordinates are the display size (1280x800 by default). Login walls: computer_login(credential=<vault name>, url=current page) — never ask the user for a password or type into password fields. Vault names on this computer: ${vault}. On handoff_pending, immediately auth_attempt_wait. You get ${ROUNDS} tool steps per turn; the conversation continues across turns, so if you run out say exactly where you stopped. Short final answer.` };
  const hist = thread;
  // A steer accepted in the turn's last round missed every drain; deliver it
  // ahead of the new prompt so nothing the user typed is lost.
  pushSteerItems(hist.items, takeSteers(thread.id));
  const userItem = { role: 'user', content: inputText };
  if (attaches.length) userItem.attaches = attaches;
  hist.items.push(userItem);
  try {
    if (auth.provider === 'anthropic') {
      const messages = histToAnthropicMessages(hydrateShots(hydrateAttaches(hist.items)), { media: true });
      const shots = new Set();
      const { text: out, finished, spend, overBudget } = await anthropicToolLoop({
        key: auth.key,
        model,
        effort,
        system: dev.content,
        messages,
        tools: ALL_TOOLS,
        emit,
        rounds: ROUNDS,
        tokenBudget: TURN_TOKEN_BUDGET,
        signal: gone.signal,
        stopped,
        beforeRound: (msgs) => {
          const nudges = takeSteers(thread.id);
          for (const n of nudges) {
            pushSteerItems(hist.items, [n]);
            appendSteerToAnthropic(msgs, n);
            emit({ type: 'steer', text: n });
          }
        },
        actFor: (name, args) => actFor(name, args || {}, id),
        runTool: async (name, args, call) => {
          claim();
          hist.items.push({
            type: 'function_call',
            call_id: call.call_id || call.id,
            name,
            arguments: call.arguments || JSON.stringify(args || {}),
          });
          const eplan = extraPlan(name, args, id);
          const result = await toolOrStop(
            () => (eplan ? runExtra(eplan) : runCaseTool(name, args, id, null)),
            actFor(name, args || {}, id));
          const { image_b64, ...persist } = result;
          hist.items.push({ type: 'function_call_output', call_id: call.call_id || call.id, output: clip(persist) });
          if (image_b64) pushShot(hist.items, shots, image_b64);
          return result;
        },
      });
      let text = out;
      if (!finished && !stopped()) {
        text = (text ? text + '\n\n' : '')
          + (overBudget
            ? `**Out of budget.** Stopped after ${spend.in.toLocaleString('en-US')} input tokens with the task unfinished. Say **continue** and I pick up from here.`
            : `**Out of steps.** Stopped after ${ROUNDS} tool calls with the task unfinished. Say **continue** and I pick up from here.`);
        emit({ type: 'text', text });
      } else if (text) {
        hist.items.push({ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] });
      }
      hist.items = histCloseOpenCalls(hist.items);
      histTrim(hist);
      thread.updated = Date.now();
      saveThreads();
      emit({ type: 'done', text, computer_id: id, thread_id: thread.id });
      return { text, computerId: id, threadId: thread.id };
    }
    const client = new OpenAI({ apiKey: auth.key });
    let text = '';
    const spend = { in: 0, cached: 0, out: 0 };
    let summary = 'detailed';
    const round = async () => {
      // The SDK leaves its abort listener on the signal after the round ends; over a
      // 200-round turn that is 200 dead listeners on one signal. A per-round
      // controller, linked for exactly the lifetime of the round, keeps it at one.
      const rc = new AbortController();
      const relay = () => rc.abort();
      gone.signal.addEventListener('abort', relay, { once: true });
      if (gone.signal.aborted) rc.abort();
      try {
      const params = {
        model, input: [dev, ...hydrateShots(hydrateAttaches(hist.items))], tools: ALL_TOOLS,
        reasoning: { effort, summary }, stream: true, store: false,
        // The loop is append-only: every round re-sends [dev, ...items], which is
        // exactly the shape the prefix cache wants. A stable key is required for
        // reliable matching; cached input bills at 0.1x.
        prompt_cache_key: thread.id,
      };
      let stream;
      try { stream = await client.responses.create(params, { signal: rc.signal }); }
      catch (err) {
        // Only the summary-unsupported fallback retries; an abort must not.
        if (summary !== 'auto' && !gone.signal.aborted) {
          summary = 'auto';
          stream = await client.responses.create({ ...params, reasoning: { effort, summary } }, { signal: rc.signal });
        } else throw err;
      }
      let response = null;
      let thinkDelta = false;
      let textDelta = false;
      const emitted = new Set();
      const names = new Map();
      for await (const ev of stream) {
        if (ev.type === 'response.completed') response = ev.response;
        if (ev.type === 'response.failed') throw new Error(ev.response?.error?.message || 'openai failed');
        const nd = streamEventToNdjson(ev);
        if (!nd) continue;
        if (nd.type === 'think_delta') thinkDelta = true;
        if (nd.type === 'think' && thinkDelta) continue;
        if (nd.type === 'text_delta') textDelta = true;
        if (nd.type === 'tool') {
          if (nd.id) { names.set(nd.id, nd.name); emitted.add(nd.id); }
          if (nd.call_id) { names.set(nd.call_id, nd.name); emitted.add(nd.call_id); }
          nd.act = actFor(nd.name, {}, id);
        }
        if (nd.type === 'tool_args' || nd.type === 'tool_args_delta') {
          nd.name = names.get(nd.id) || nd.name;
          if (nd.type === 'tool_args') nd.act = actFor(nd.name || 'tool', nd.args || {}, id);
        }
        emit(nd);
      }
      if (!response && typeof stream.finalResponse === 'function') response = await stream.finalResponse();
      if (!response) throw new Error('openai stream ended with no response');
      const traces = tracesFromOutput(response.output);
      if (!thinkDelta) for (const t of traces.thinks) emit({ type: 'think', text: t });
      for (const call of traces.calls) {
        const cId = call.id || call.call_id;
        if (emitted.has(cId) || emitted.has(call.call_id)) continue;
        let args = {};
        try { args = JSON.parse(call.arguments || '{}'); } catch { args = {}; }
        emit({ type: 'tool', id: cId, call_id: call.call_id || cId, name: call.name, act: actFor(call.name, args, id), args });
      }
      return { response, traces, textDelta };
      } finally {
        gone.signal.removeEventListener('abort', relay);
      }
    };
    let finished = false;
    const shots = new Set();      // screenshot hashes already in this turn's history
    const snaps = { last: '' };   // hash of the most recent snapshot's element list
    const overBudget = () => spend.in > TURN_TOKEN_BUDGET;
    let i = 0;
    for (; i < ROUNDS && !finished && !stopped() && !overBudget(); i++) {
      const nudges = takeSteers(thread.id);
      if (nudges.length) {
        for (const n of nudges) {
          pushSteerItems(hist.items, [n]);
          emit({ type: 'steer', text: n });
        }
      }
      const { response, traces, textDelta } = await withRateRetry(round, emit, 5, gone.signal);
      const u = response.usage || {};
      spend.in += u.input_tokens || 0;
      spend.cached += u.input_tokens_details?.cached_tokens || 0;
      spend.out += u.output_tokens || 0;
      text = (response.output_text || traces.texts.join('\n') || '').trim();
      if (!traces.calls.length) {
        finished = true;
        // The answering round must land in history too, or the reply exists
        // only in the stream: reloads show bare prompts and the model never
        // sees what it already said.
        hist.items.push(...response.output);
        if (text && !textDelta) emit({ type: 'text', text });
        break;
      }
      hist.items.push(...response.output);
      claim();   // first tool call = the run needs hands: the task finds its agent
      const images = [];
      for (const call of traces.calls) {
        let args = {};
        try { args = JSON.parse(call.arguments || '{}'); } catch { args = {}; }
        const act = actFor(call.name, args, id);
        const callId = call.call_id || call.id;
        emit({ type: 'tool_run', id: call.id, call_id: callId, name: call.name, act });
        const eplan = extraPlan(call.name, args, id);
        const result = await toolOrStop(
          () => (eplan ? runExtra(eplan) : runCaseTool(call.name, args, id)), act);
        const { image_b64, ...rest } = result;
        emit({ type: 'tool_result', id: call.id, call_id: callId, act: rest.act || act, ok: !!rest.ok, name: call.name, args_used: call.name === 'computer_exec' ? String(args.command || '').slice(0, 200) : undefined, detail: clip(rest.error || rest.result || rest, 400) });
        hist.items.push({ type: 'function_call_output', call_id: callId, output: clip(snapshotElide(call.name, rest, snaps)) });
        if (image_b64) images.push(image_b64);
      }
      hist.items = histCloseOpenCalls(hist.items, { keepReasoning: true });
      for (const b64 of images) pushShot(hist.items, shots, b64);
    }
    if (!finished && !stopped()) {
      // Out of steps or out of budget mid-task. Say so — silence here reads as
      // "it just stopped" — and the carried history makes "continue" actually resume.
      text = (text ? text + '\n\n' : '')
        + (overBudget()
          ? `**Out of budget.** Stopped after ${spend.in.toLocaleString('en-US')} input tokens with the task unfinished. Say **continue** and I pick up from here.`
          : `**Out of steps.** Stopped after ${ROUNDS} tool calls with the task unfinished. Say **continue** and I pick up from here.`);
      emit({ type: 'text', text });
    }
    // Reasoning items are only valid inside the turn that produced them; carrying
    // them forward bloats the payload and some models reject stale ones.
    hist.items = histCloseOpenCalls(hist.items);
    histTrim(hist);
    thread.updated = Date.now();
    saveThreads();
    const eff = Math.round((spend.in - spend.cached) + 0.1 * spend.cached);
    console.log(`drive turn ${thread.id}: in=${spend.in} cached=${spend.cached}`
      + ` (${spend.in ? Math.round((100 * spend.cached) / spend.in) : 0}%)`
      + ` eff=${eff} out=${spend.out} rounds=${i}`);
    spend.eff = eff;
    emit({ type: 'done', text, computer_id: id, thread_id: thread.id, spend, rounds: i });
    return { text, computerId: id, threadId: thread.id };
  } catch (err) {
    // Keep the turn even on provider errors: tools already ran, that work is
    // real. histCloseOpenCalls synthesizes outputs for any dangling
    // function_call so later requests don't 400. (Rate limits are retried
    // inside the round; landing here means retries ran dry or a real fault.)
    hist.items = histCloseOpenCalls(hist.items);
    histTrim(hist);
    thread.updated = Date.now();
    saveThreads();
    if (!stopped()) emit({ type: 'error', error: (err?.message || 'provider error') + ' — say continue, I pick up where I stopped.' });
    return { text: '', computerId: id, threadId: thread.id, error: err?.message || 'provider error' };
  } finally {
    const leftover = takeSteers(thread.id);
    if (leftover.length) {
      pushSteerItems(thread.items, leftover);
      thread.updated = Date.now();
      saveThreads();
    }
  }
}

async function chat(req, res) {
  const buf = await readBody(req, res);
  if (!buf) return;
  let body;
  try { body = JSON.parse(buf.toString('utf8') || '{}'); }
  catch { return send(res, 400, 'bad json'); }
  const auth = chatAuth(req.headers);
  if (!auth.key) return send(res, 401, 'missing key');
  const model = resolveChatModel(body.model, auth.provider);
  const effort = ['none', 'low', 'medium', 'high', 'xhigh', 'max'].includes(body.effort) ? body.effort : 'medium';
  const inputText = String(body.input || '').slice(0, 32000);
  const fileIds = Array.isArray(body.files) ? body.files.slice(0, ATTACH_MAX_N) : [];
  const attaches = [];
  for (const ref of fileIds) {
    const rec = resolveAttach(typeof ref === 'string' ? ref : ref?.id);
    if (!rec) return send(res, 400, 'attachment not found');
    attaches.push({ path: rec.path, name: rec.name, mime: rec.mime });
  }
  if (!inputText && !attaches.length) return send(res, 400, 'empty input');
  const picked = String(body.computer_id || '');
  const thread = THREADS.get(String(body.thread_id || '')) || newThread(inputText || attaches[0].name, picked);
  if (CHAT_BUSY.has(thread.id)) return json(res, 409, { error: 'this thread is still running a turn' });
  CHAT_BUSY.add(thread.id);
  const stopped = () => res.destroyed;
  const emit = (obj) => {
    if (stopped() || res.destroyed) return;
    if (!res.headersSent) {
      res.writeHead(200, {
        'content-type': 'application/x-ndjson; charset=utf-8',
        'cache-control': 'no-store', 'x-accel-buffering': 'no',
      });
    }
    try { res.write(JSON.stringify(obj) + '\n'); } catch { /* client gone */ }
  };
  let clientGone;
  const goneP = new Promise((r) => { clientGone = r; });
  const gone = new AbortController();
  res.on('close', () => { clientGone(); gone.abort(); });
  try {
    await runTurn({
      thread, inputText, attaches, auth, computerId: picked, model, effort,
      emit, stopped, signal: gone.signal, disconnect: goneP,
    });
  } catch (err) {
    if (!res.headersSent) return json(res, err.status || 500, { error: err.message || 'internal' });
    if (!stopped()) emit({ type: 'error', error: err.message || 'internal' });
  } finally {
    CHAT_BUSY.delete(thread.id);
    if (res.headersSent && !res.writableEnded) res.end();
  }
}

async function pendingHandoffs() {
  const r = await api('GET', '/handoffs?status=pending', { timeoutMs: 8000 });
  return r.json?.handoffs || [];
}

async function pendingHandoffIds() {
  return (await pendingHandoffs()).map((r) => r.id).filter(Boolean);
}

async function answerHandoff(hid, value) {
  return api('POST', `/handoffs/${encodeURIComponent(hid)}/answer`, {
    json: { value }, timeoutMs: 120000,
  });
}

const ANSWERED = { approve: 'Approved.', deny: 'Denied.' };

async function phoneContext() {
  const thread = phoneThread();
  let pendingIds = [];
  try { pendingIds = await pendingHandoffIds(); }
  catch (err) { console.warn('phone handoffs:', err.message || err); }
  return { thread, pendingIds, busy: CHAT_BUSY.has(thread.id) };
}

/** Carry out a routed phone decision. say(kind, text) is the transport's
 *  reply; kind ∈ error | answered | queued | working | done. */
async function phoneAct(decision, { auth, model, say }) {
  const thread = phoneThread();
  if (decision.type === 'ignore') return;
  if (decision.type === 'error') return say('error', decision.error);
  if (decision.type === 'handoff') {
    try {
      const r = await answerHandoff(decision.hid, decision.value);
      if (r.status >= 400) {
        return say('error', r.json?.error?.message || `handoff ${r.status}`);
      }
      return say('answered', ANSWERED[decision.value] || 'Submitted.');
    } catch (err) {
      return say('error', err.message || 'handoff failed');
    }
  }
  if (decision.type === 'steer') {
    const q = STEER.get(thread.id) || [];
    q.push(decision.text);
    STEER.set(thread.id, q);
    return say('queued', 'Queued on the running turn');
  }
  let computerId;
  try { computerId = await cid(); }
  catch (err) { return say('error', err.message || 'cased unreachable'); }
  if (!computerId) return say('error', 'no computer — create one first');
  if (CHAT_BUSY.has(thread.id)) return say('error', 'this thread is still running a turn');
  CHAT_BUSY.add(thread.id);
  await say('working', 'Working');
  let finalText = '';
  let errText = '';
  const emit = (obj) => {
    if (obj?.type === 'done') finalText = obj.text || finalText;
    if (obj?.type === 'text' && obj.text) finalText = obj.text;
    if (obj?.type === 'error') errText = obj.error || 'provider error';
  };
  try {
    const result = await runTurn({
      thread, inputText: decision.text, attaches: [], auth, computerId,
      model, effort: 'medium', emit, stopped: () => false,
    });
    if (result?.error) errText = result.error;
    if (result?.text) finalText = result.text;
  } catch (err) {
    errText = err.message || 'turn failed';
  } finally {
    CHAT_BUSY.delete(thread.id);
  }
  if (errText) return say('error', errText);
  return say('done', finalText || 'done');
}

const NTFY_TITLES = {
  error: '[Case] error', answered: '[Case] answered', queued: '[Case] queued',
  working: '[Case] working', done: '[Case] done',
};

async function onPhoneMessage(cfg, auth, model, text) {
  const { pendingIds, busy } = await phoneContext();
  const say = (kind, message) => ntfy.publish(cfg, { title: NTFY_TITLES[kind], message }).catch((err) => {
    console.warn('ntfy publish:', err.message || err);
  });
  return phoneAct(routePhone({ text, pendingIds, busy }), { auth, model, say });
}

export function startPhoneNtfy(env = process.env) {
  const cfg = ntfy.ntfyConfig(env);
  if (!cfg.chat) return false;
  if (!cfg.topic) {
    console.warn('CASE_NTFY_CHAT=1 but CASE_NTFY_TOPIC unset');
    return false;
  }
  const auth = envDriveAuth(env);
  if (!auth.key) {
    console.warn('CASE_NTFY_CHAT=1 but CASE_DRIVE_API_KEY unset');
    return false;
  }
  const model = resolveChatModel(env.CASE_DRIVE_MODEL || '', auth.provider);
  ntfy.listen(cfg, (text) => onPhoneMessage(cfg, auth, model, text));
  console.log(`drive ntfy chat on ${cfg.url}`);
  return true;
}

// ---------- phone: Telegram ----------
/** cased /v1/events framing: "event: <type>\ndata: <json>\n\n"; comments are
 *  heartbeats. Returns parsed blocks plus the unterminated tail. */
export function sseEvents(chunk, carry = '') {
  const blocks = (carry + chunk).split('\n\n');
  const rest = blocks.pop() ?? '';
  const events = [];
  for (const b of blocks) {
    const event = /^event: (.+)$/m.exec(b)?.[1];
    const data = /^data: (.+)$/m.exec(b)?.[1];
    if (!event || !data) continue;
    try { events.push({ event, data: JSON.parse(data) }); } catch { /* malformed */ }
  }
  return { events, rest };
}

/** Follow cased's event stream and call onHandoff for each new handoff.
 *  Reconnects forever; cased restarts must not silence the phone. */
function watchHandoffs(onHandoff, onConnect) {
  let timer = null;
  const retry = () => { clearTimeout(timer); timer = setTimeout(connect, 5000); };
  const connect = () => {
    const rq = http.get({
      hostname: CASE.hostname, port: CASE.port, path: '/v1/events',
      headers: { accept: 'text/event-stream', ...(TOKEN ? { authorization: 'Bearer ' + TOKEN } : {}) },
    }, (rs) => {
      if (rs.statusCode !== 200) console.warn('cased events:', rs.statusCode);
      else onConnect();
      let carry = '';
      rs.setEncoding('utf8');
      rs.on('data', (chunk) => {
        const parsed = sseEvents(chunk, carry);
        carry = parsed.rest;
        for (const { event, data } of parsed.events) {
          if (event === 'handoff_created') onHandoff(data);
        }
      });
      rs.on('error', () => { /* close follows */ });
      rs.on('close', retry);
    });
    rq.on('error', retry);
  };
  connect();
}

// Force-reply prompt message id → handoff id, so a reply names its handoff.
const CODE_PROMPTS = new Map();
// Handoff ids already sent to the phone, so a reconnect replay does not repeat
// them. Ids answered from the Drive UI linger; handoffs are rare, it is bytes.
const PUSHED = new Set();

async function pushHandoff(cfg, h) {
  if (PUSHED.has(h.id)) return;
  PUSHED.add(h.id);
  const m = telegram.handoffMessage(h);
  try {
    const sent = await telegram.tgApi(cfg.token, 'sendMessage', { chat_id: cfg.chatId, ...m });
    if (m.reply_markup.force_reply) CODE_PROMPTS.set(Number(sent.message_id), h.id);
  } catch (err) {
    PUSHED.delete(h.id);
    console.warn('telegram handoff:', err.message || err);
  }
}

/** Reply function for phoneAct: "working" shows the typing indicator until
 *  the next reply; everything else is text, split at Telegram's limit. */
function telegramSay(cfg) {
  let typing = null;
  const action = () => telegram.tgApi(cfg.token, 'sendChatAction', { chat_id: cfg.chatId, action: 'typing' })
    .catch(() => { /* cosmetic */ });
  return async (kind, text) => {
    clearInterval(typing);
    typing = null;
    if (kind === 'working') {
      action();
      typing = setInterval(action, 4500);
      return;
    }
    for (const part of telegram.chunk(kind === 'error' ? `Error: ${text}` : text)) {
      await telegram.tgApi(cfg.token, 'sendMessage', { chat_id: cfg.chatId, text: part })
        .catch((err) => console.warn('telegram send:', err.message || err));
    }
  };
}

async function onTelegramUpdate(cfg, auth, model, update) {
  const parsed = telegram.parseUpdate(update);
  if (!parsed) return;
  const { chatId, msg } = parsed;
  if (chatId !== cfg.chatId) {
    if (!cfg.chatId && msg.kind === 'text' && msg.text === '/start') {
      await telegram.tgApi(cfg.token, 'sendMessage', {
        chat_id: chatId,
        text: `This chat's id is ${chatId}. Put CASE_TELEGRAM_CHAT_ID=${chatId} in .env and restart the ui container.`,
      });
    }
    return;
  }
  if (msg.kind === 'callback') {
    await telegram.tgApi(cfg.token, 'answerCallbackQuery', { callback_query_id: msg.callbackId })
      .catch(() => { /* spinner only */ });
  }
  const { pendingIds, busy } = await phoneContext();
  const decision = telegram.routeTelegram({ msg, codePrompts: CODE_PROMPTS, pendingIds, busy });
  const say = telegramSay(cfg);
  if (decision.type === 'start') return say('done', 'Paired. Send a task, or answer a prompt here.');
  if (decision.type === 'stale') {
    return say('error', `skipped a message sent ${Math.round(decision.age / 60)} min ago while Drive was down:\n${decision.text.slice(0, 300)}\nSend it again if you still want it.`);
  }
  if (decision.type === 'handoff') {
    for (const [mid, hid] of CODE_PROMPTS) if (hid === decision.hid) CODE_PROMPTS.delete(mid);
    PUSHED.delete(decision.hid);
  }
  return phoneAct(decision, { auth, model, say });
}

export function startPhoneTelegram(env = process.env) {
  const cfg = telegram.telegramConfig(env);
  if (!cfg.token) {
    if (env.CASE_TELEGRAM_TOKEN) console.warn('CASE_TELEGRAM_TOKEN is not a BotFather token');
    return false;
  }
  const auth = envDriveAuth(env);
  if (!auth.key) {
    console.warn('CASE_TELEGRAM_TOKEN set but CASE_DRIVE_API_KEY unset');
    return false;
  }
  const model = resolveChatModel(env.CASE_DRIVE_MODEL || '', auth.provider);
  // A webhook left on the bot makes getUpdates 409 forever; clearing it is idempotent.
  telegram.tgApi(cfg.token, 'deleteWebhook', {}).catch(() => { /* poll reports 409 if this failed */ })
    .then(() => telegram.poll(cfg, (u) => onTelegramUpdate(cfg, auth, model, u)));
  if (cfg.chatId) {
    watchHandoffs((h) => pushHandoff(cfg, h), () => {
      pendingHandoffs().then((rows) => rows.forEach((h) => pushHandoff(cfg, h)))
        .catch((err) => console.warn('phone handoffs:', err.message || err));
    });
  }
  console.log(cfg.chatId
    ? 'drive telegram chat on'
    : 'drive telegram: send /start to the bot, then set CASE_TELEGRAM_CHAT_ID');
  return true;
}

// ---------- live view (relayed by cased; only cased touches desktops) ----------
const LIVE_PASS = ['accept', 'accept-language', 'user-agent', 'if-none-match', 'if-modified-since'];
const LIVE_WS_PASS = ['connection', 'upgrade', 'sec-websocket-key', 'sec-websocket-version',
  'sec-websocket-protocol', 'sec-websocket-extensions'];
// Allowlist, not {...req.headers}: the browser's Drive cookie/Authorization are
// Drive's credentials and must not ride upstream.
export function liveHeaders(req, ws = false, token = TOKEN) {
  const out = { host: `${CASE.hostname}:${CASE.port}` };
  for (const k of ws ? [...LIVE_PASS, ...LIVE_WS_PASS] : LIVE_PASS) if (req.headers[k] != null) out[k] = req.headers[k];
  if (token) out.authorization = `Bearer ${token}`;
  return out;
}
function vncUpstream(req) {
  if (livePathHasDotDot(req.url)) return null;
  const destPath = liveDestPath(req.url);
  const cid = liveCid(new URL(req.url || '/', 'http://x').pathname) || cachedCid;
  if (!cid) return null;
  return { hostname: CASE.hostname, port: CASE.port, path: `/v1/computers/${encodeURIComponent(cid)}/live${destPath}` };
}
function vncHttp(req, res) {
  const t = vncUpstream(req);
  if (!t) { res.writeHead(502).end('no computer / vnc'); return; }
  const up = http.request({ hostname: t.hostname, port: t.port, path: t.path, method: req.method, headers: liveHeaders(req) }, (upRes) => {
    res.writeHead(upRes.statusCode || 502, upRes.headers);
    upRes.pipe(res);
  });
  up.on('error', () => { if (!res.headersSent) res.writeHead(502).end('desk unreachable'); else res.destroy(); });
  req.pipe(up);
}
function vncWs(req, socket, head) {
  const t = vncUpstream(req);
  if (!t) { socket.destroy(); return; }
  const up = http.request({ hostname: t.hostname, port: t.port, path: t.path, method: 'GET', headers: liveHeaders(req, true) });
  up.on('upgrade', (upRes, upSocket, upHead) => {
    const lines = ['HTTP/1.1 101 Switching Protocols'];
    for (const [k, v] of Object.entries(upRes.headers)) lines.push(`${k}: ${Array.isArray(v) ? v.join(', ') : v}`);
    socket.write(lines.join('\r\n') + '\r\n\r\n');
    if (upHead?.length) socket.write(upHead);
    upSocket.pipe(socket);
    socket.pipe(upSocket);
    for (const s of [socket, upSocket]) s.on('error', () => { socket.destroy(); upSocket.destroy(); });
  });
  up.on('error', () => socket.destroy());
  up.end();
  if (head?.length) up.write(head);
}

// Only the two pages are servable. Everything else in this directory —
// server source, thread history — is not a web asset; the request falls
// through to 404 rather than trusting a proxy to filter paths.
const PAGE_FILES = { '/': '/index.html', '/index.html': '/index.html', '/deploy': '/deploy.html', '/deploy/': '/deploy.html', '/deploy.html': '/deploy.html' };
export function pageFile(p) {
  return PAGE_FILES[p] || '';
}

export const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://x');
  const p = url.pathname;
  if (!browserOk(req)) return json(res, 403, { error: 'unexpected Host or Origin' });
  if (TOKEN && req.method === 'GET' && url.searchParams.has('token') && tokenMatches(req)) {
    res.writeHead(302, {
      Location: p === '/' ? '/' : p,
      'Set-Cookie': `case_token=${encodeURIComponent(TOKEN)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000`,
      'Cache-Control': 'no-store',
    });
    return res.end();
  }
  if (!tokenMatches(req)) {
    if (p.startsWith('/api/') || p.startsWith('/live')) {
      return json(res, 401, { error: 'unauthorized' });
    }
    return send(res, 401, 'unauthorized — open with ?token=…');
  }
  if (p === '/api/health' && req.method === 'GET') {
    try {
      const h = await originHealth();
      return json(res, 200, {
        ok: true, live: CASE.hostname, up: h.status === 200, local: LOCAL,
        max_running: Number(h.json?.max_running) || 0,
        running: Number(h.json?.running) || 0,
        computers: Number(h.json?.computers) || 0,
        docker: !!h.json?.docker,
      });
    } catch {
      return json(res, 200, { ok: true, live: CASE.hostname, up: false, local: LOCAL, max_running: 0, running: 0 });
    }
  }
  try {
    if (req.method === 'GET' && p === '/api/computers') return computers(res);
    if (req.method === 'POST' && p === '/api/computers') return createComputer(res, req);
    if (req.method === 'DELETE' && p === '/api/computers') return deleteComputer(res, req);
    if (req.method === 'GET' && p === '/api/fs') return fsList(res, url);
    if (p === '/api/creds') return creds(req, res, url);
    if (p === '/api/threads') return threadsRoute(req, res, url);
    if (req.method === 'GET' && p === '/api/file') return fsFile(res, url);
    if (req.method === 'POST' && p === '/api/chat') return chat(req, res);
    if (req.method === 'POST' && p === '/api/chat/steer') return steer(req, res);
    if (req.method === 'POST' && p === '/api/attach') return attach(req, res);
    if (req.method === 'POST' && p === '/api/teach-tick') {
      const id = await cid();
      if (!id) return json(res, 409, { error: 'no computer' });
      const r = await api('POST', `/computers/${encodeURIComponent(id)}/teach-tick?wake=true`, { timeoutMs: 15000 });
      return json(res, r.status >= 400 ? r.status : 200, r.json || {});
    }
    if (req.method === 'POST' && p === '/api/wake') return power(res, req, 'wake');
    if (req.method === 'POST' && p === '/api/sleep') return power(res, req, 'sleep');
    if (p.startsWith('/live')) return vncHttp(req, res);
  } catch (err) {
    return json(res, 500, { error: err.message || 'internal' });
  }
  const file = pageFile(p);
  const abs = path.normalize(path.join(DIR, file));
  if (!abs.startsWith(DIR) || !fs.existsSync(abs) || fs.statSync(abs).isDirectory()) {
    return send(res, 404, 'not found');
  }
  send(res, 200, fs.readFileSync(abs), mimeFor(abs));
});
server.on('upgrade', (req, socket, head) => {
  if (!browserOk(req) || !tokenMatches(req)) { socket.destroy(); return; }
  if ((req.url || '').startsWith('/live')) return vncWs(req, socket, head);
  socket.destroy();
});

const isMain = fileURLToPath(import.meta.url) === path.resolve(process.argv[1] || '');
if (isMain) {
  server.on('clientError', (_e, s) => { try { s.destroy(); } catch { /* gone */ } });
  server.listen(PORT, BIND, () => {
    process.stdout.write(`drive http://${BIND}:${PORT}/  deploy http://${BIND}:${PORT}/deploy  (cased ${CASE.hostname}:${CASE.port}${LOCAL ? ', local' : ''})\n`);
    startPhoneNtfy();
    startPhoneTelegram();
  });
}
