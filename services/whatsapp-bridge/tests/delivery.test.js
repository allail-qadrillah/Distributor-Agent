import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DeliveryStore } from '../src/delivery-store.js';
import { classifyReceipt } from '../src/receipts.js';
import { DeliveryReporter } from '../src/delivery-reporter.js';

const logger = { info() {}, warn() {}, error() {} };

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-delivery-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const mapping = { messageId: '3EB0ABC123', outboxId: '0b2f6d3e-1111-2222-3333-444455556666', to: '628111000001@s.whatsapp.net' };

test('save persists the mapping before any send can happen', () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  assert.equal(store.lookup('3EB0ABC123').outboxId, mapping.outboxId);
  assert.equal(store.lookup('3EB0ABC123').status, null);
  assert.equal(store.lookup('3EB0DEAD'), null);
});

test('setStatus is durable across a restart', () => {
  const first = new DeliveryStore({ dataDir: root, logger });
  first.save(mapping);
  first.setStatus('3EB0ABC123', 'delivered');
  const second = new DeliveryStore({ dataDir: root, logger });
  assert.equal(second.lookup('3EB0ABC123').status, 'delivered');
});

test('remove deletes the mapping', () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  store.setStatus('3EB0ABC123', 'failed');
  store.remove('3EB0ABC123');
  assert.equal(store.lookup('3EB0ABC123'), null);
  assert.equal(readdirSync(join(root, 'outbound')).length, 0);
});

test('pending lists only mappings with a classified status', () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  store.save({ ...mapping, messageId: '3EB0DEF789', outboxId: 'u2' });
  store.setStatus('3EB0ABC123', 'delivered');
  const pending = store.pending();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].messageId, '3EB0ABC123');
  assert.deepEqual(store.counts(), { mappings: 2, pendingReports: 1 });
});

test('classifyReceipt follows the status table', () => {
  const entry = (status, fromMe = true) => ({
    key: { id: '3EB0X', fromMe, remoteJid: '628111000001@s.whatsapp.net' },
    update: { status },
  });
  assert.equal(classifyReceipt(entry(0)), 'failed');
  assert.equal(classifyReceipt(entry(3)), 'delivered');
  assert.equal(classifyReceipt(entry(4)), 'delivered');
  assert.equal(classifyReceipt(entry(5)), 'delivered');
  assert.equal(classifyReceipt(entry(1)), null);
  assert.equal(classifyReceipt(entry(2)), null);
  assert.equal(classifyReceipt(entry(99)), null, 'unrecognized status holds the mapping');
  assert.equal(classifyReceipt(entry(3, false)), null, 'inbound receipts are not ours');
  assert.equal(classifyReceipt(null), null);
  assert.equal(classifyReceipt({ key: { fromMe: true }, update: {} }), null);
});

const waitFor = async (fn, timeoutMs = 2000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return fn();
};

function makeClient(handler) {
  return {
    reports: [],
    handler,
    async postDeliveryReport(report) {
      this.reports.push(report);
      return this.handler(report, this.reports.length);
    },
  };
}

test('observe records the receipt, reporter posts it and clears the mapping', async () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  const client = makeClient(() => ({ ok: true, status: 200 }));
  const reporter = new DeliveryReporter({ store, client, logger, pollMs: 2, backoff: () => 0 });
  reporter.observe([{ key: { id: '3EB0ABC123', fromMe: true, remoteJid: '628111000001@s.whatsapp.net' }, update: { status: 3 } }]);
  assert.equal(store.lookup('3EB0ABC123').status, 'delivered');
  reporter.start();
  assert.ok(await waitFor(() => store.lookup('3EB0ABC123') === null));
  await reporter.stop();
  assert.equal(client.reports[0].outbox_id, mapping.outboxId);
  assert.equal(client.reports[0].status, 'delivered');
  assert.ok(client.reports[0].reported_at);
});

test('non-2xx keeps the mapping and retries until acknowledged', async () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  store.setStatus('3EB0ABC123', 'failed');
  const client = makeClient((_, attempt) => (attempt === 1 ? { ok: false, status: 503 } : { ok: true, status: 200 }));
  const reporter = new DeliveryReporter({ store, client, logger, pollMs: 2, backoff: () => 0 });
  reporter.start();
  assert.ok(await waitFor(() => client.reports.length >= 2 && store.lookup('3EB0ABC123') === null));
  await reporter.stop();
  assert.equal(client.reports[1].status, 'failed');
});

test('receipts for untracked messages are ignored', async () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  const client = makeClient(() => ({ ok: true, status: 200 }));
  const reporter = new DeliveryReporter({ store, client, logger, pollMs: 2, backoff: () => 0 });
  reporter.observe([{ key: { id: 'UNKNOWN', fromMe: true, remoteJid: 'x' }, update: { status: 3 } }]);
  reporter.start();
  await new Promise((r) => setTimeout(r, 30));
  await reporter.stop();
  assert.equal(client.reports.length, 0);
});

test('pending mappings on disk are reported after a restart', async () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save(mapping);
  store.setStatus('3EB0ABC123', 'delivered');
  const restarted = new DeliveryStore({ dataDir: root, logger });
  const client = makeClient(() => ({ ok: true, status: 200 }));
  const reporter = new DeliveryReporter({ store: restarted, client, logger, pollMs: 2, backoff: () => 0 });
  reporter.start();
  assert.ok(await waitFor(() => restarted.lookup('3EB0ABC123') === null));
  await reporter.stop();
  assert.equal(client.reports.length, 1);
});
