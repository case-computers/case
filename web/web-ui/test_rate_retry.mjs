#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Pure-unit checks for the rate-limit retry. Run: node web/web-ui/test_rate_retry.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import Anthropic from '@anthropic-ai/sdk';
import OpenAI from 'openai';
import { isRateLimited, rateWaitS, withRateRetry } from './case-tools.mjs';

// --- what counts as "too fast" -------------------------------------------
assert.ok(isRateLimited({ status: 429 }));
assert.ok(isRateLimited({ status: 529 }));
assert.ok(isRateLimited({ response: { status: 429 } }));
assert.ok(isRateLimited({ message: 'Rate limit reached for gpt-5.6' }));
assert.ok(isRateLimited({ message: 'Overloaded' }));
assert.ok(!isRateLimited({ status: 400, message: "unsupported value: 'detailed'" }));
assert.ok(!isRateLimited({ status: 401 }));
assert.ok(!isRateLimited(undefined));

// --- how long to wait ----------------------------------------------------
// the server's own hint wins over the exponential guess
assert.equal(rateWaitS({ message: 'try again in 12s' }, 0), 12.5);
assert.equal(rateWaitS({ headers: { 'retry-after': '9' } }, 0), 9.5);
assert.equal(rateWaitS({ response: { headers: new Map([['retry-after', '4']]) } }, 0), 4.5);
// what the SDKs actually throw: APIError.headers is a fetch Headers object
{
  const h = new Headers({ 'retry-after': '30' });
  assert.equal(rateWaitS(Anthropic.APIError.generate(429, { error: { type: 'rate_limit_error', message: 'slow' } }, 'slow', h), 0), 30.5);
  assert.equal(rateWaitS(OpenAI.APIError.generate(429, { message: 'slow' }, 'slow', h), 0), 30.5);
  assert.equal(rateWaitS({ status: 429, headers: new Headers() }, 1), 2.5, 'no header: exponential');
}
// no hint: exponential in the attempt number
assert.equal(rateWaitS({ status: 429 }, 0), 1.5);
assert.equal(rateWaitS({ status: 429 }, 3), 8.5);
// clamped both ends — never busy-loop, never park for an hour
assert.equal(rateWaitS({ message: 'try again in 0.01s' }, 0), 1);
assert.equal(rateWaitS({ message: 'try again in 3600s' }, 0), 60);
assert.equal(rateWaitS({ status: 429 }, 20), 60);

// --- the loop ------------------------------------------------------------
// a non-rate-limit error is not retried: one call, straight out
let calls = 0;
await assert.rejects(
  () => withRateRetry(async () => { calls++; throw Object.assign(new Error('bad request'), { status: 400 }); }),
  /bad request/);
assert.equal(calls, 1, 'a 400 must not be retried');

// a rate limit is retried and the flow continues where it left off
calls = 0;
const seen = [];
const limited = Object.assign(new Error('rate limit — try again in 0.01s'), { status: 429 });
const out = await withRateRetry(async () => {
  calls++;
  if (calls < 3) throw limited;
  return 'round done';
}, (ev) => seen.push(ev));
assert.equal(out, 'round done');
assert.equal(calls, 3);
assert.equal(seen.length, 2, 'one notice per wait');
assert.ok(seen.every((e) => e.type === 'think' && e.rate === true), 'notices carry rate:true so headless consumers can log them');
assert.match(seen[0].text, /rate limited — retrying in 1s/);

// retries do run dry — the caller has to see the error, not hang forever
calls = 0;
await assert.rejects(() => withRateRetry(async () => {
  calls++;
  throw limited;
}, null, 3), /rate limit/);
assert.equal(calls, 3, 'tries is a hard cap');

// STOP / disconnect must cancel the backoff sleep, not wait it out
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

// --- the two error fallbacks must not eat a 429 --------------------------
// Both retry a round with a param removed when the model rejects it. Neither may
// fire on a rate limit: no backoff, and serve.mjs's `summary` is sticky for the turn.
const serve = fs.readFileSync(new URL('./serve.mjs', import.meta.url), 'utf8');
assert.match(serve, /if \(gone\.signal\.aborted\) throw err;/);
assert.match(serve, /if \(isRateLimited\(err\)\) throw err;/);
assert.match(serve, /else if \(summary !== 'auto'\)/);
const tools = fs.readFileSync(new URL('./case-tools.mjs', import.meta.url), 'utf8');
assert.match(tools, /!params\.output_config \|\| isRateLimited\(err\)/);

console.log('rate retry ok');
