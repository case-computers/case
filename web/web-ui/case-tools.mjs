// SPDX-License-Identifier: MIT
/**
 * The chat tool loop: the four computer_* tools the Drive UI hands to the model,
 * how each maps onto a cased REST call, and how Responses API stream events
 * become Drive NDJSON lines.
 *
 * Talks to cased over CASE_URL only (loopback, or `cased` on the compose
 * network). Never logs CASE_TOKEN.
 */
import http from 'node:http';
import https from 'node:https';
import Anthropic from '@anthropic-ai/sdk';

export const CASE_TOOLS = [
  { type: 'function', name: 'computer_navigate', description: 'Point the computer browser at url and block until the page has loaded. Returns {ok, url, title, text, snapshot} — text is the first 2000 chars of the page; snapshot holds the numbered elements, so do NOT call computer_snapshot after this. Same-page #anchor jumps are not navigations.', parameters: { type: 'object', properties: { url: { type: 'string' }, timeout_s: { type: 'number' } }, required: ['url'], additionalProperties: false } },
  { type: 'function', name: 'computer_eval', description: 'Evaluate JS in the active tab (CDP, promises awaited). Prefer this over screenshots for page content. Return plain values — DOM nodes are not serialisable. To read a page as prose, document.body.innerText. Do not drive location.assign from here; use computer_navigate.', parameters: { type: 'object', properties: { expression: { type: 'string' }, timeout_s: { type: 'number' } }, required: ['expression'], additionalProperties: false } },
  { type: 'function', name: 'computer_action', description: 'UI action on the desktop (1280x800 by default): click|double_click|move|drag|scroll|type|key|wait. Coordinates are pixels, origin top-left. keys uses xdotool syntax (ctrl+l, Return). For elements INSIDE a web page prefer computer_snapshot + computer_click_element; use this for the desktop itself, canvas, shortcuts, and scrolling.', parameters: { type: 'object', properties: { type: { type: 'string', enum: ['click', 'double_click', 'move', 'drag', 'scroll', 'type', 'key', 'wait'] }, x: { type: 'number' }, y: { type: 'number' }, button: { type: 'string', enum: ['left', 'middle', 'right'] }, text: { type: 'string' }, keys: { type: 'string' }, dy: { type: 'number' }, ms: { type: 'number' }, from_x: { type: 'number' }, from_y: { type: 'number' }, to_x: { type: 'number' }, to_y: { type: 'number' } }, required: ['type'], additionalProperties: false } },
  { type: 'function', name: 'computer_exec', description: 'Run a shell command on the computer (bash, as user agent).', parameters: { type: 'object', properties: { command: { type: 'string' }, timeout_s: { type: 'number' } }, required: ['command'], additionalProperties: false } },
];

function caseRoot() {
  const raw = String(process.env.CASE_URL || 'http://127.0.0.1:8787/v1').trim().replace(/\/$/, '');
  return /\/v1$/.test(raw) ? raw : `${raw}/v1`;
}

export function caseToolPlan(name, args, cid) {
  const a = args && typeof args === 'object' ? args : {};
  const id = encodeURIComponent(cid);
  if (name === 'computer_navigate') {
    const t = Math.min(Math.max(Number(a.timeout_s) || 30, 1), 120);
    return { method: 'POST', path: `/computers/${id}/navigate?wake=true`, json: { url: a.url, timeout_s: t }, timeoutMs: (t + 20) * 1000, act: `navigate ${a.url || ''}` };
  }
  if (name === 'computer_eval') {
    const t = Math.min(Math.max(Number(a.timeout_s) || 20, 1), 120);
    return { method: 'POST', path: `/computers/${id}/eval?wake=true`, json: { expression: a.expression, timeout_s: t }, timeoutMs: (t + 20) * 1000, act: 'eval' };
  }
  if (name === 'computer_action') {
    const json = { type: a.type, screenshot: false };
    for (const k of ['x', 'y', 'button', 'text', 'keys', 'dy', 'ms']) {
      if (a[k] != null) json[k] = a[k];
    }
    if (a.type === 'drag') {
      json.from = { x: a.from_x, y: a.from_y };
      json.to = { x: a.to_x, y: a.to_y };
    }
    return { method: 'POST', path: `/computers/${id}/action?wake=true`, json, timeoutMs: 30000, act: `action ${a.type || ''}` };
  }
  if (name === 'computer_exec') {
    const t = Math.min(Math.max(Number(a.timeout_s) || 30, 1), 600);
    // Spool the full output to a file inside the computer and show the model only the
    // head of it. A noisy command otherwise lands whole in history and is re-sent on
    // every later round of the turn; the file keeps the rest reachable with cat/grep/tail.
    // The exit code has to be echoed explicitly — the redirect swallows it otherwise.
    const log = `/tmp/case-out-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}.log`;
    // Subshell, not a brace group: `exit 1` in the agent's command would otherwise
    // kill this shell before the exit line is echoed, and `{ cmd\n; }` is a syntax error.
    const command = `( ${a.command}\n) > ${log} 2>&1; __rc=$?; echo "exit=$__rc";`
      + ` wc -l < ${log} | tr -d ' ' | sed 's/^/lines=/'; head -c 1500 ${log};`
      + ` find /tmp -name 'case-out-*.log' -mmin +120 -delete 2>/dev/null || true`;
    return { method: 'POST', path: `/computers/${id}/exec?wake=true`, json: { command, timeout_s: t }, timeoutMs: (t + 30) * 1000, act: 'exec', logPath: log };
  }
  return { error: `unknown tool ${name}` };
}

// The one HTTP client for cased in JS: the chat tool loop and serve.mjs both
// route through it. json/body are interchangeable JSON payload keys; rawBody
// sends bytes as-is; raw:true resolves {status, buf} instead of parsed JSON.
// maxBytes stops reading a bigger reply and resolves {status, tooBig: true}.
export function caseCall(method, rel, { json, body, rawBody = null, raw = false, timeoutMs = 20000, maxBytes = 0 } = {}) {
  const u = new URL(rel.startsWith('http') ? rel : caseRoot() + rel);
  const lib = u.protocol === 'https:' ? https : http;
  const data = json ?? body;
  const payload = rawBody != null ? rawBody : (data == null ? null : Buffer.from(JSON.stringify(data)));
  const token = (process.env.CASE_TOKEN || '').trim();
  return new Promise((resolve, reject) => {
    const req = lib.request({
      hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      method,
      path: u.pathname + u.search,
      headers: {
        accept: 'application/json',
        ...(token ? { authorization: 'Bearer ' + token } : {}),
        ...(payload ? { 'content-type': 'application/json', 'content-length': String(payload.length) } : {}),
      },
    }, (res) => {
      const chunks = [];
      let n = 0;
      const tooBig = () => { res.destroy(); resolve({ status: res.statusCode || 0, tooBig: true }); };
      if (maxBytes && Number(res.headers['content-length']) > maxBytes) return tooBig();
      res.on('data', (c) => {
        n += c.length;
        if (maxBytes && n > maxBytes) return tooBig();
        chunks.push(c);
      });
      res.on('end', () => {
        const buf = Buffer.concat(chunks);
        if (raw) return resolve({ status: res.statusCode || 0, buf });
        const text = buf.toString('utf8');
        let parsed = null;
        try { parsed = JSON.parse(text); } catch { /* not json */ }
        resolve({ status: res.statusCode || 0, json: parsed, raw: text.slice(0, 400) });
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

export async function runCaseTool(name, args, cid) {
  const plan = caseToolPlan(name, args, cid);
  if (plan.error) return { ok: false, error: plan.error, act: name };
  try {
    const r = await caseCall(plan.method, plan.path, plan);
    if (r.status >= 400) {
      return { ok: false, status: r.status, error: r.json || r.raw, act: plan.act };
    }
    const result = r.json ?? { ok: true };
    if (plan.logPath && result && typeof result === 'object') {
      result.full_output = `${plan.logPath} — only the first 1500 bytes are above; cat/grep/tail this file for the rest`;
    }
    return { ok: true, act: plan.act, result };
  } catch (err) {
    return { ok: false, error: err.message || 'cased unreachable', act: plan.act };
  }
}

export function tracesFromOutput(output) {
  const thinks = [];
  const calls = [];
  const texts = [];
  for (const x of output || []) {
    if (x.type === 'reasoning') {
      for (const s of x.summary || []) {
        const t = typeof s === 'string' ? s : s.text;
        if (t && String(t).trim()) thinks.push(String(t).trim());
      }
    } else if (x.type === 'function_call') calls.push(x);
    else if (x.type === 'message') {
      for (const c of x.content || []) {
        if ((c.type === 'output_text' || c.type === 'text') && c.text) texts.push(c.text);
      }
    }
  }
  return { thinks, calls, texts };
}

/** Map one Responses API stream event to a Drive NDJSON line, or null. */
export function streamEventToNdjson(ev) {
  const t = ev?.type;
  if (t === 'response.reasoning_summary_text.delta' && ev.delta) {
    return { type: 'think_delta', text: ev.delta };
  }
  if (t === 'response.reasoning_summary_text.done' && ev.text) {
    return { type: 'think', text: ev.text };
  }
  if (t === 'response.reasoning_summary_part.done' && ev.part?.text) {
    return { type: 'think', text: ev.part.text };
  }
  if (t === 'response.output_item.added' && ev.item?.type === 'function_call') {
    return {
      type: 'tool',
      id: ev.item.id || ev.item.call_id,
      call_id: ev.item.call_id || ev.item.id,
      name: ev.item.name,
    };
  }
  if (t === 'response.function_call_arguments.delta' && ev.delta) {
    return { type: 'tool_args_delta', id: ev.item_id, text: ev.delta };
  }
  if (t === 'response.function_call_arguments.done') {
    let args = {};
    try { args = JSON.parse(ev.arguments || '{}'); } catch { args = {}; }
    return { type: 'tool_args', id: ev.item_id, args };
  }
  if (t === 'response.output_text.delta' && ev.delta) {
    return { type: 'text_delta', text: ev.delta };
  }
  return null;
}

const MODELS = {
  'gpt-5.6': 'gpt-5.6',
  'gpt-5.6-sol': 'gpt-5.6-sol',
  'gpt-5.6-terra': 'gpt-5.6-terra',
  'gpt-5.6-luna': 'gpt-5.6-luna',
};
export const ANTHROPIC_MODELS = {
  'claude-opus-4-6': 'claude-opus-4-6',
  'claude-sonnet-4-6': 'claude-sonnet-4-6',
  'claude-haiku-4-5': 'claude-haiku-4-5',
};

export function chatAuth(headers = {}) {
  const anthropic = String(headers['x-anthropic-key'] || '').trim();
  const openai = String(headers['x-openai-key'] || '').trim();
  if (anthropic) return { provider: 'anthropic', key: anthropic };
  if (openai) return { provider: 'openai', key: openai };
  return { provider: '', key: '' };
}

export function envDriveAuth(env = process.env) {
  const key = String(env.CASE_DRIVE_API_KEY || '').trim();
  const provider = String(env.CASE_DRIVE_PROVIDER || '').trim().toLowerCase();
  if (!key || (provider !== 'openai' && provider !== 'anthropic')) return { provider: '', key: '' };
  return { provider, key };
}

export function resolveChatModel(requested, provider) {
  if (provider === 'anthropic') return ANTHROPIC_MODELS[requested] || 'claude-sonnet-4-6';
  return MODELS[requested] || 'gpt-5.6-terra';
}

export function openaiToolsToAnthropic(tools) {
  return (tools || []).map((t) => ({
    name: t.name,
    description: t.description || '',
    input_schema: t.parameters || { type: 'object', properties: {} },
  }));
}

export function newAnthropicStreamCtx() {
  return { names: new Map(), ids: new Map(), json: new Map() };
}

export function anthropicEventToNdjson(ev, ctx) {
  if (!ev || !ctx) return null;
  if (ev.type === 'content_block_start') {
    const b = ev.content_block || {};
    if (b.type !== 'tool_use') return null;
    ctx.names.set(ev.index, b.name);
    ctx.ids.set(ev.index, b.id);
    ctx.json.set(ev.index, '');
    return { type: 'tool', id: b.id, call_id: b.id, name: b.name };
  }
  if (ev.type === 'content_block_delta') {
    const d = ev.delta || {};
    if (d.type === 'thinking_delta' && d.thinking) return { type: 'think_delta', text: d.thinking };
    if (d.type === 'text_delta' && d.text) return { type: 'text_delta', text: d.text };
    if (d.type === 'input_json_delta' && d.partial_json) {
      ctx.json.set(ev.index, (ctx.json.get(ev.index) || '') + d.partial_json);
      return {
        type: 'tool_args_delta',
        id: ctx.ids.get(ev.index),
        text: d.partial_json,
        name: ctx.names.get(ev.index),
      };
    }
    return null;
  }
  if (ev.type === 'content_block_stop') {
    if (!ctx.json.has(ev.index)) return null;
    let args = {};
    try { args = JSON.parse(ctx.json.get(ev.index) || '{}'); } catch { args = {}; }
    return {
      type: 'tool_args',
      id: ctx.ids.get(ev.index),
      args,
      name: ctx.names.get(ev.index),
    };
  }
  return null;
}

export function tracesFromAnthropicMessage(message) {
  const thinks = [];
  const calls = [];
  const texts = [];
  for (const b of message?.content || []) {
    if (b.type === 'thinking' && b.thinking) thinks.push(b.thinking);
    else if (b.type === 'tool_use') {
      calls.push({
        id: b.id,
        call_id: b.id,
        name: b.name,
        arguments: JSON.stringify(b.input || {}),
      });
    } else if (b.type === 'text' && b.text) texts.push(b.text);
  }
  return { thinks, calls, texts };
}

function userContentText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return content == null ? '' : String(content);
  return content.map((c) => {
    if (typeof c === 'string') return c;
    if (c?.type === 'input_text' || c?.type === 'text') return c.text || '';
    return '';
  }).filter(Boolean).join('\n');
}

function openaiPartToAnthropic(c) {
  if (typeof c === 'string') return c ? { type: 'text', text: c } : null;
  if (c?.type === 'input_text' || c?.type === 'text') return c.text ? { type: 'text', text: c.text } : null;
  if (c?.type === 'input_image') {
    const m = /^data:(image\/[a-z0-9.+-]+);base64,(.+)$/i.exec(c.image_url || '');
    if (!m) return null;
    return { type: 'image', source: { type: 'base64', media_type: m[1], data: m[2] } };
  }
  if (c?.type === 'input_file') {
    const m = /^data:application\/pdf;base64,(.+)$/i.exec(c.file_data || '');
    if (!m) return c.filename ? { type: 'text', text: '[' + c.filename + ']' } : null;
    return { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: m[1] } };
  }
  return null;
}

export function histToAnthropicMessages(items, { media = false } = {}) {
  const messages = [];
  let pendingAssistant = [];
  let pendingResults = [];
  const flushAssistant = () => {
    if (!pendingAssistant.length) return;
    messages.push({ role: 'assistant', content: pendingAssistant });
    pendingAssistant = [];
  };
  const flushResults = () => {
    if (!pendingResults.length) return;
    messages.push({ role: 'user', content: pendingResults });
    pendingResults = [];
  };
  for (const it of items || []) {
    if (it.shot && !media) continue;
    if (it.role === 'user' && it.content != null && !it.type) {
      flushAssistant();
      flushResults();
      if (media && Array.isArray(it.content)) {
        const parts = it.content.map(openaiPartToAnthropic).filter(Boolean);
        if (parts.length) {
          messages.push({
            role: 'user',
            content: parts.length === 1 && parts[0].type === 'text' ? parts[0].text : parts,
          });
          continue;
        }
      }
      const text = userContentText(it.content);
      if (text) messages.push({ role: 'user', content: text });
    } else if (it.type === 'function_call') {
      flushResults();
      let input = {};
      try { input = JSON.parse(it.arguments || '{}'); } catch { input = {}; }
      pendingAssistant.push({
        type: 'tool_use',
        id: it.call_id || it.id,
        name: it.name,
        input,
      });
    } else if (it.type === 'function_call_output') {
      flushAssistant();
      pendingResults.push({
        type: 'tool_result',
        tool_use_id: it.call_id,
        content: String(it.output || ''),
      });
    } else if (it.type === 'message' && it.role === 'assistant') {
      flushResults();
      const text = (it.content || []).map((c) => c.text || '').join('');
      if (text) pendingAssistant.push({ type: 'text', text });
    }
  }
  flushAssistant();
  flushResults();
  return messages;
}

export function anthropicThinkingFor(messages) {
  const assistants = (messages || []).filter((m) => m.role === 'assistant');
  if (!assistants.length) return { type: 'adaptive' };
  const hasThinking = assistants.some((m) => Array.isArray(m.content)
    && m.content.some((b) => b.type === 'thinking' || b.type === 'redacted_thinking'));
  return hasThinking ? { type: 'adaptive' } : { type: 'disabled' };
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

/** Is this the provider saying "too fast" rather than "bad request"? Callers with
 * their own error fallbacks (unsupported summary/effort) must ask first: a 429
 * misread as an unsupported-param error retries with no backoff and degrades the
 * request for nothing. */
export function isRateLimited(err) {
  const status = err?.status ?? err?.response?.status;
  return status === 429 || status === 529
    || /rate limit|overloaded/i.test(err?.message || '');
}

/** Worth replaying the round unchanged: too fast, or a transient provider or
 * network fault (what the SDKs retry by default). Both clients run with
 * maxRetries: 0, so withRateRetry is the only retry policy. */
export function isRetryable(err) {
  const status = err?.status ?? err?.response?.status;
  if (isRateLimited(err) || status === 408 || status === 409 || status >= 500) return true;
  return status == null && /connection error|timed out/i.test(err?.message || '');
}

/** Seconds to wait before attempt `a`: the server's own hint ("try again in Xs"
 * or retry-after) if it gave one, else exponential. Padded, clamped to 1..60s. */
export function rateWaitS(err, a) {
  const m = /try again in ([\d.]+)s/i.exec(err?.message || '');
  // Both SDKs' APIError.headers is a fetch Headers; plain objects still pass.
  const header = (h) => (typeof h?.get === 'function' ? h.get('retry-after') : h?.['retry-after']) ?? undefined;
  const hdr = Number(header(err?.headers) ?? header(err?.response?.headers));
  const wait = m ? Number(m[1]) : Number.isFinite(hdr) && hdr > 0 ? hdr : 2 ** a;
  return Math.min(Math.max(wait + 0.5, 1), 60);
}

/** Retry a provider round on rate limits (429/529) and transient faults,
 * honoring the server's suggested wait. History is only mutated after a round
 * completes, so replaying a failed round is safe. `signal` cancels the backoff
 * sleep on STOP. */
function abortError(signal) {
  if (signal?.reason instanceof Error) return signal.reason;
  const err = new Error(signal?.reason ? String(signal.reason) : 'stopped by user');
  err.name = 'AbortError';
  return err;
}

function abortableDelay(ms, signal) {
  if (!signal) return new Promise((resolve) => setTimeout(resolve, ms));
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const done = () => {
      signal.removeEventListener('abort', stop);
      resolve();
    };
    const stop = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', stop);
      reject(abortError(signal));
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', stop, { once: true });
    if (signal.aborted) stop();
  });
}

export async function withRateRetry(fn, emit, tries = 5, signal) {
  // What was streamed is not replay-safe: a round that fails mid-stream has already
  // emitted text and tool rows. `round` marks its start, `round_reset` rolls back to it.
  emit?.({ type: 'round' });
  for (let a = 0; ; a++) {
    if (signal?.aborted) throw abortError(signal);
    try { return await fn(); }
    catch (err) {
      if (signal?.aborted) throw err;
      if (!isRetryable(err) || a >= tries - 1) throw err;
      const wait = rateWaitS(err, a);
      const why = isRateLimited(err) ? 'rate limited' : 'provider error';
      emit?.({ type: 'round_reset' });
      // `rate: true` so a non-UI consumer can pick the wait out of the think stream.
      emit?.({ type: 'think', rate: true, text: `${why} — retrying in ${Math.ceil(wait)}s` });
      await abortableDelay(wait * 1000, signal);
    }
  }
}

export async function anthropicToolLoop({
  key, model, effort, system, messages, tools, emit, rounds, runTool, actFor, stopped,
  beforeRound, tokenBudget, signal,
}) {
  const client = new Anthropic({ apiKey: key, maxRetries: 0 });
  const antTools = openaiToolsToAnthropic(tools);
  const antEffort = effort === 'none' ? 'low' : (effort || 'medium');
  const params = {
    model,
    max_tokens: 16384,
    system,
    messages,
    tools: antTools,
    thinking: anthropicThinkingFor(messages),
    output_config: { effort: antEffort },
    cache_control: { type: 'ephemeral' },
  };
  let text = '';
  let finished = false;
  // eff is billed input, the number the budget bounds (serve.mjs counts OpenAI's
  // the same way): input_tokens excludes the cache, whose reads bill at 0.1x and
  // writes at 1.25x. Raw input counts every cached re-send in full.
  const spend = { in: 0, cached: 0, out: 0, eff: 0 };
  const overBudget = () => Number(tokenBudget) > 0 && spend.eff > tokenBudget;
  const round = async (p) => {
    const ctx = newAnthropicStreamCtx();
    let thinkDelta = false;
    let textDelta = false;
    const stream = client.messages.stream(p);
    const abort = () => stream.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    try {
      for await (const ev of stream) {
        const nd = anthropicEventToNdjson(ev, ctx);
        if (!nd) continue;
        if (nd.type === 'think_delta') thinkDelta = true;
        if (nd.type === 'text_delta') textDelta = true;
        if (nd.type === 'tool') nd.act = actFor(nd.name, {}) || nd.name;
        if (nd.type === 'tool_args') nd.act = actFor(nd.name || 'tool', nd.args || {}) || nd.name;
        emit(nd);
      }
      const message = await stream.finalMessage();
      const traces = tracesFromAnthropicMessage(message);
      if (!thinkDelta) {
        for (const t of traces.thinks) emit({ type: 'think', text: t });
      }
      return { message, traces, textDelta };
    } finally {
      signal?.removeEventListener('abort', abort);
    }
  };
  for (let i = 0; i < rounds && !finished && !overBudget(); i++) {
    if (stopped?.()) break;
    beforeRound?.(messages, { round: i, eff: spend.eff });
    let result;
    try {
      result = await withRateRetry(() => round(params), emit, 5, signal);
    } catch (err) {
      if (signal?.aborted) throw err;
      // This fallback is for models that reject output_config — not for a rate
      // limit or fault whose retries already ran dry, which would only buy 5 more waits.
      if (!params.output_config || isRetryable(err)) throw err;
      const rest = { ...params };
      delete rest.output_config;
      emit({ type: 'round_reset' });
      result = await withRateRetry(() => round(rest), emit, 5, signal);
    }
    const { message, traces, textDelta } = result;
    const u = message.usage || {};
    const read = u.cache_read_input_tokens || 0;
    const wrote = u.cache_creation_input_tokens || 0;
    spend.in += (u.input_tokens || 0) + read + wrote;
    spend.cached += read;
    spend.eff += (u.input_tokens || 0) + 1.25 * wrote + 0.1 * read;
    spend.out += u.output_tokens || 0;
    text = traces.texts.join('\n').trim();
    if (!traces.calls.length) {
      finished = true;
      if (text && !textDelta) emit({ type: 'text', text });
      break;
    }
    messages.push({ role: 'assistant', content: message.content });
    const results = [];
    for (const call of traces.calls) {
      let args = {};
      try { args = JSON.parse(call.arguments || '{}'); } catch { args = {}; }
      const act = actFor(call.name, args) || call.name;
      emit({ type: 'tool_run', id: call.id, call_id: call.call_id, name: call.name, act });
      const toolResult = await runTool(call.name, args, call);
      emit({
        type: 'tool_result',
        id: call.id,
        call_id: call.call_id,
        act: toolResult.act || act,
        ok: !!toolResult.ok,
        detail: clip(toolResult.error || toolResult.result || toolResult, 400),
      });
      const { image_b64, ...rest } = toolResult;
      const content = [{ type: 'text', text: clip(rest) }];
      if (image_b64) content.push({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: image_b64 } });
      results.push({ type: 'tool_result', tool_use_id: call.call_id || call.id, content });
    }
    messages.push({ role: 'user', content: results });
  }
  return { text, finished, spend, overBudget: overBudget() };
}
