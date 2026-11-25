// SPDX-License-Identifier: MIT
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const PROVIDERS = ['openai', 'anthropic'];
const MIN_KEY_LENGTH = 8;
const LABEL_MAX = 40;

export function secretsHome(home) {
  return home || process.env.CASE_HOME || path.join(process.env.HOME || '/tmp', '.case');
}

const keyPath = (home) => path.join(secretsHome(home), 'provider-secrets.key');
const storePath = (home) => path.join(secretsHome(home), 'provider-secrets.json');

function loadKey(home) {
  const p = keyPath(home);
  if (!fs.existsSync(p)) {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, crypto.randomBytes(32), { mode: 0o600 });
  }
  const k = fs.readFileSync(p);
  if (k.length !== 32) throw new Error('bad provider key file');
  fs.chmodSync(p, 0o600);
  return k;
}

function readStore(home) {
  try { return JSON.parse(fs.readFileSync(storePath(home), 'utf8')); }
  catch { return {}; }
}

function writeStore(home, obj) {
  const p = storePath(home);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = p + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(obj), { mode: 0o600 });
  fs.renameSync(tmp, p);
}

function requireProvider(provider) {
  if (!PROVIDERS.includes(provider)) throw new Error('bad_provider');
}

export function putProviderKey(provider, plaintext, home, { label = '' } = {}) {
  requireProvider(provider);
  const raw = String(plaintext || '').trim();
  if (raw.length < MIN_KEY_LENGTH) throw new Error('short_key');
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', loadKey(home), iv);
  const ct = Buffer.concat([cipher.update(raw, 'utf8'), cipher.final()]);
  const db = readStore(home);
  db[provider] = {
    ct: ct.toString('base64'),
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    label: String(label || '').trim().slice(0, LABEL_MAX),
    last4: raw.slice(-4),
    saved_at: new Date().toISOString(),
  };
  writeStore(home, db);
  return status(home);
}

export function deleteProviderKey(provider, home) {
  requireProvider(provider);
  const db = readStore(home);
  delete db[provider];
  writeStore(home, db);
  return status(home);
}

function decrypt(row, home) {
  const decipher = crypto.createDecipheriv('aes-256-gcm', loadKey(home), Buffer.from(row.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(row.tag, 'base64'));
  return Buffer.concat([decipher.update(Buffer.from(row.ct, 'base64')), decipher.final()]).toString('utf8');
}

export function getProviderKey(provider, home) {
  requireProvider(provider);
  const row = readStore(home)[provider];
  return row ? decrypt(row, home) : '';
}

export function newestProviderKey(home) {
  const db = readStore(home);
  const rows = PROVIDERS.filter((p) => db[p]).sort((a, b) => db[b].saved_at.localeCompare(db[a].saved_at));
  for (const provider of rows) {
    try { return { provider, key: decrypt(db[provider], home), saved_at: db[provider].saved_at }; }
    catch { continue; }
  }
  return null;
}

export function status(home) {
  const db = readStore(home);
  const out = {};
  for (const provider of PROVIDERS) {
    const row = db[provider];
    out[provider] = row
      ? { configured: true, provider, label: row.label, last4: row.last4, saved_at: row.saved_at }
      : { configured: false };
  }
  return out;
}
