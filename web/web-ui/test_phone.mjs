#!/usr/bin/env node
// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import { PHONE_THREAD_ID, parseHandoffReply, routePhone } from './phone.mjs';

assert.equal(PHONE_THREAD_ID, 't_phone');

assert.deepEqual(parseHandoffReply('h_abc 482910'), { hid: 'h_abc', value: '482910' });
assert.deepEqual(parseHandoffReply('approve'), { hid: null, value: 'approve' });

assert.deepEqual(routePhone({ text: 'h_1 approve', pendingIds: ['h_1'] }),
  { type: 'handoff', hid: 'h_1', value: 'approve' });
assert.deepEqual(routePhone({ text: '482910', pendingIds: ['h_9'] }),
  { type: 'handoff', hid: 'h_9', value: '482910' });
assert.equal(routePhone({ text: 'ok', pendingIds: ['h_1', 'h_2'] }).type, 'error');
assert.equal(routePhone({ text: 'h_nope x', pendingIds: ['h_1'] }).type, 'error');
assert.equal(routePhone({ text: 'approve', pendingIds: [] }).error, 'Nothing waiting.');
assert.equal(routePhone({ text: 'done', pendingIds: [] }).error, 'Nothing waiting.');
assert.equal(routePhone({ text: '123456', pendingIds: [] }).error, 'Nothing waiting.');
assert.deepEqual(routePhone({ text: 'check gmail', pendingIds: [], busy: true }),
  { type: 'steer', text: 'check gmail' });
assert.deepEqual(routePhone({ text: 'check gmail', pendingIds: [] }),
  { type: 'task', text: 'check gmail' });
assert.equal(routePhone({ text: 'check gmail', pendingIds: ['h_1'] }).type, 'handoff');
assert.equal(routePhone({ text: '' }).type, 'ignore');

console.log('ok test_phone.mjs');
