#!/usr/bin/env node
// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

process.env.CASE_URL = process.env.CASE_URL || 'http://127.0.0.1:1';
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'case-push-'));
const threadsFile = path.join(home, 'threads.json');
process.env.CASE_THREADS = threadsFile;
const { vapidKeys, pushSubs, addPushSub, dropPushSub, pushBody, gcShots } = await import('./serve.mjs');

const keys = vapidKeys(home);
assert.ok(keys.publicKey && keys.privateKey, 'keys generated');
assert.deepEqual(vapidKeys(home), keys, 'generated once, then reused');
const vapidFile = path.join(home, 'drive', 'vapid.json');
assert.equal(fs.statSync(vapidFile).mode & 0o777, 0o600, 'private key is 0600');

const sub = (endpoint) => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } });
assert.equal(addPushSub(sub('https://fcm.googleapis.com/fcm/send/1'), 'ios', home), 1);
assert.equal(addPushSub(sub('https://fcm.googleapis.com/fcm/send/2'), 'android', home), 2);
assert.equal(addPushSub(sub('https://fcm.googleapis.com/fcm/send/1'), 'ios again', home), 2, 'dedupe by endpoint');
assert.equal(addPushSub({ endpoint: 'https://fcm.googleapis.com/fcm/send/3' }, '', home), null, 'a subscription without keys is refused');
assert.equal(addPushSub(sub('http://push/plain'), '', home), null, 'a plain http endpoint is refused');
assert.equal(addPushSub(sub('https://10.0.0.5/x'), '', home), null, 'a host that is not a push service is refused');
assert.equal(dropPushSub('https://fcm.googleapis.com/fcm/send/1', home), 1);
assert.equal(pushSubs(home).some((s) => s.endpoint === 'https://fcm.googleapis.com/fcm/send/1'), false);
assert.equal(fs.statSync(path.join(home, 'drive', 'push.json')).mode & 0o777, 0o600, 'subscriptions are 0600');

for (let i = 0; i < 25; i++) addPushSub(sub('https://fcm.googleapis.com/fcm/send/n' + i), '', home);
assert.equal(pushSubs(home).length, 20, 'only the newest 20 endpoints are kept');

const shots = fs.mkdtempSync(path.join(os.tmpdir(), 'case-push-shots-'));
const kept = path.join(shots, 'a'.repeat(40) + '.png');
const stale = path.join(shots, 'b'.repeat(40) + '.png');
fs.writeFileSync(kept, 'kept');
fs.writeFileSync(stale, 'stale');
fs.writeFileSync(threadsFile, JSON.stringify([{ items: [{ card: 1, shot: kept }] }]));
gcShots(shots, 1);
assert.ok(fs.existsSync(kept), 'persisted screenshot card survives cleanup');
assert.ok(!fs.existsSync(stale), 'unreferenced screenshot is evicted');
fs.rmSync(shots, { recursive: true, force: true });

for (const [kind, domain] of [['otp', 'accounts.google.com'], ['approval', 'x.com'], ['captcha', ''], ['question', 'bob@evil.com'], ['device', 'https://a/b?token=zz']]) {
  const body = pushBody(kind, domain);
  assert.ok(!/https?:/i.test(body), 'no url on a lock screen: ' + body);
  assert.ok(!body.includes('@'), 'no username on a lock screen: ' + body);
  assert.ok(body.length < 60, 'one line: ' + body);
}
assert.equal(pushBody('otp', 'accounts.google.com'), 'accounts.google.com needs a code');
assert.equal(pushBody('approval', 'x.com'), 'x.com needs a yes or no');
assert.equal(pushBody('captcha', ''), 'your computer needs you on the screen');

fs.rmSync(home, { recursive: true, force: true });
console.log('web push: all checks pass');
