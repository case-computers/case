#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  putProviderKey, getProviderKey, deleteProviderKey, newestProviderKey, status,
} from './provider-secrets.mjs';

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'case-keys-'));
const store = () => fs.readFileSync(path.join(home, 'provider-secrets.json'), 'utf8');
const openai = 'sk-test-openai-secret-value';
const anthropic = 'sk-ant-test-anthropic-secret-value';

assert.deepEqual(status(home), { openai: { configured: false }, anthropic: { configured: false } });
assert.equal(newestProviderKey(home), null);

const st1 = putProviderKey('openai', openai, home, { label: 'work' });
assert.equal(st1.openai.configured, true);
assert.equal(st1.openai.provider, 'openai');
assert.equal(st1.openai.label, 'work');
assert.equal(st1.openai.last4, openai.slice(-4));
assert.ok(Date.parse(st1.openai.saved_at));
assert.deepEqual(Object.keys(st1.openai).sort(), ['configured', 'label', 'last4', 'provider', 'saved_at']);
assert.equal(st1.anthropic.configured, false);
assert.ok(!JSON.stringify(st1).includes(openai));
assert.equal(getProviderKey('openai', home), openai);
assert.equal(getProviderKey('anthropic', home), '');
assert.ok(!store().includes(openai));

await new Promise((r) => setTimeout(r, 5));
const st2 = putProviderKey('anthropic', anthropic, home);
assert.equal(st2.anthropic.label, '');
assert.equal(getProviderKey('anthropic', home), anthropic);
assert.ok(!JSON.stringify(st2).includes(anthropic));
assert.ok(!store().includes(anthropic));
assert.equal(newestProviderKey(home).provider, 'anthropic');
assert.equal(newestProviderKey(home).key, anthropic);

await new Promise((r) => setTimeout(r, 5));
putProviderKey('openai', 'sk-replaced-key-value', home);
assert.equal(getProviderKey('openai', home), 'sk-replaced-key-value');
assert.equal(newestProviderKey(home).provider, 'openai');
assert.ok(!store().includes('sk-replaced-key-value'));

assert.equal(deleteProviderKey('openai', home).openai.configured, false);
assert.equal(getProviderKey('openai', home), '');
assert.equal(newestProviderKey(home).provider, 'anthropic');

const keyFile = path.join(home, 'provider-secrets.key');
assert.equal(fs.statSync(keyFile).mode & 0o777, 0o600);
assert.equal(fs.statSync(path.join(home, 'provider-secrets.json')).mode & 0o777, 0o600);

fs.writeFileSync(keyFile, Buffer.alloc(32, 7));
assert.equal(newestProviderKey(home), null);
assert.throws(() => getProviderKey('anthropic', home));

assert.throws(() => putProviderKey('nope', 'sk-xxxxxxxx', home), /bad_provider/);
assert.throws(() => putProviderKey('openai', 'short', home), /short_key/);

fs.rmSync(home, { recursive: true, force: true });
console.log('provider-secrets: all checks pass');
