import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DeliveryStore } from '../src/delivery-store.js';
import { OutboundSender, phoneToJid, validateOutboundRequest } from '../src/outbound.js';

const logger = { info() {}, warn() {}, error() {} };

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-outbound-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const payload = { outbox_id: '0b2f6d3e-aaaa-bbbb-cccc-dddddddddddd', to: '+628111000001', text: 'halo' };

function makeSender({ send, waitLimit = 10, sendTimeoutMs = 15_000, generateId } = {}) {
  const store = new DeliveryStore({ dataDir: root, logger });
  const sender = new OutboundSender({
    store,
    logger,
    send: send ?? (async () => {}),
    waitLimit,
    sendTimeoutMs,
    generateId: generateId ?? (() => '3EB0TESTID'),
  });
  return { sender, store };
}

test('phoneToJid maps E.164 and bare numbers', () => {
  assert.equal(phoneToJid('+628111000001'), '628111000001@s.whatsapp.net');
  assert.equal(phoneToJid('628111000001'), '628111000001@s.whatsapp.net');
  assert.equal(phoneToJid('abc'), null);
  assert.equal(phoneToJid(''), null);
  assert.equal(phoneToJid(null), null);
  assert.equal(phoneToJid('+628'), null);
});

test('validation rejects bad payloads with named errors', () => {
  assert.equal(validateOutboundRequest(null), 'invalid_request');
  assert.equal(validateOutboundRequest({ ...payload, outbox_id: '' }), 'invalid_outbox_id');
  assert.equal(validateOutboundRequest({ ...payload, outbox_id: 5 }), 'invalid_outbox_id');
  assert.equal(validateOutboundRequest({ ...payload, to: 'nope' }), 'invalid_to');
  assert.equal(validateOutboundRequest({ ...payload, text: '  ' }), 'invalid_text');
  assert.equal(validateOutboundRequest(payload), null);
});

test('happy path: mapping persisted before send, 200 sent', async () => {
  const order = [];
  const store = new DeliveryStore({ dataDir: root, logger });
  const sender = new OutboundSender({
    store,
    logger,
    send: async (jid, text, messageId) => {
      order.push(['send', messageId, store.lookup(messageId) != null]);
    },
    generateId: () => '3EB0SEQ1',
  });
  const result = await sender.handle(payload);
  assert.deepEqual(order, [['send', '3EB0SEQ1', true]], 'mapping exists before sendMessage');
  assert.equal(result.status, 200);
  assert.equal(result.body.status, 'sent');
  assert.equal(result.body.message_id, '3EB0SEQ1');
  assert.equal(store.lookup('3EB0SEQ1').outboxId, payload.outbox_id);
});

test('send failure: mapping removed, 200 failed', async () => {
  const { sender, store } = makeSender({ send: async () => { throw new Error('connection closed'); } });
  const result = await sender.handle(payload);
  assert.equal(result.status, 200);
  assert.deepEqual(result.body, { status: 'failed', error: 'send_failed' });
  assert.equal(store.lookup('3EB0TESTID'), null);
});

test('send timeout: mapping kept for reconciliation, 500 unknown', async () => {
  const { sender, store } = makeSender({ send: () => new Promise(() => {}), sendTimeoutMs: 20 });
  const result = await sender.handle(payload);
  assert.equal(result.status, 500);
  assert.equal(result.body.status, 'unknown');
  assert.ok(store.lookup('3EB0TESTID'), 'mapping survives an uncertain send');
});

test('gate: one send in flight, others wait, past the limit 429', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { sender } = makeSender({ send: async () => { await gate; } });
  const first = sender.handle(payload);
  const waiters = [];
  for (let i = 0; i < 10; i++) {
    waiters.push(sender.handle({ ...payload, outbox_id: `waiting-${i}` }));
  }
  assert.equal(sender.waiting, 10);
  const rejected = await sender.handle({ ...payload, outbox_id: 'overflow' });
  assert.equal(rejected.status, 429);
  assert.equal(rejected.body.error, 'busy');
  release();
  const results = await Promise.all([first, ...waiters]);
  assert.ok(results.every((r) => r.status === 200));
  assert.equal(sender.waiting, 0);
});

test('shutdown refuses new sends without touching the gate', async () => {
  const { sender } = makeSender();
  sender.beginShutdown();
  const result = await sender.handle(payload);
  assert.equal(result.status, 200);
  assert.deepEqual(result.body, { status: 'failed', error: 'shutting_down' });
});

test('invalid requests are rejected before the gate', async () => {
  const { sender } = makeSender();
  const result = await sender.handle({ outbox_id: 'x', to: 'bad', text: '' });
  assert.equal(result.status, 400);
  assert.equal(result.body.status, 'failed');
});
