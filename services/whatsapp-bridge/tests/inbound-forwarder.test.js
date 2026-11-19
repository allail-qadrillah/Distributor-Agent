import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { InboundStore } from '../src/inbound-store.js';
import { InboundForwarder } from '../src/inbound-forwarder.js';

const logger = { info() {}, warn() {}, error() {} };

let root;
const activeForwarders = [];
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-fwd-'));
});
test.afterEach(async () => {
  for (const forwarder of activeForwarders.splice(0)) {
    await forwarder.stop();
  }
  rmSync(root, { recursive: true, force: true });
});

const msg = (id, jid = '628111000001@s.whatsapp.net') => ({
  key: { remoteJid: jid, id, fromMe: false },
  message: { conversation: `pesan ${id}` },
  messageTimestamp: 1_700_000_000,
});

function makeClient(handler) {
  const calls = [];
  const client = {
    calls,
    handler,
    async postEvents(payload) {
      this.calls.push(payload);
      return this.handler(payload, this.calls.length);
    },
  };
  return client;
}

function makeForwarder({ client, store, markRead, backoff = () => 0 } = {}) {
  const s = store ?? new InboundStore({ dataDir: root, logger });
  const forwarder = new InboundForwarder({
    store: s,
    client,
    logger,
    markRead,
    pollMs: 2,
    backoff,
  });
  activeForwarders.push(forwarder);
  return { forwarder, store: s };
}

const waitFor = async (fn, timeoutMs = 2000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return fn();
};

test('delivers records to the events endpoint and marks them done', async () => {
  const client = makeClient(() => ({ status: 200, ok: true, body: { results: [{ status: 'stored', mark_read: false }] }, retryAfterMs: null }));
  const { forwarder, store } = makeForwarder({ client });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0));
  await forwarder.stop();
  assert.equal(client.calls[0].messages.length, 1);
  assert.equal(client.calls[0].messages[0].key.id, 'A1');
});

test('mark_read acks trigger the read receipt before the record is finished', async () => {
  const readKeys = [];
  const client = makeClient(() => ({ status: 200, ok: true, body: { results: [{ status: 'stored', mark_read: true }] }, retryAfterMs: null }));
  const { forwarder, store } = makeForwarder({ client, markRead: async (key) => readKeys.push(key) });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().processing === 0 && store.counts().retry === 0));
  await forwarder.stop();
  assert.deepEqual(readKeys, [{ remoteJid: '628111000001@s.whatsapp.net', id: 'A1', fromMe: false }]);
});

test('read-receipt failure retries the record', async () => {
  let attempts = 0;
  const client = makeClient(() => {
    attempts += 1;
    return { status: 200, ok: true, body: { results: [{ status: 'stored', mark_read: true }] }, retryAfterMs: null };
  });
  const readKeys = [];
  const markRead = async (key) => {
    readKeys.push(key);
    if (readKeys.length === 1) throw new Error('socket not connected');
  };
  const { forwarder, store } = makeForwarder({ client, markRead });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => readKeys.length >= 2 && store.counts().retry === 0 && store.counts().processing === 0));
  await forwarder.stop();
  assert.ok(attempts >= 2, 'message redelivered after the receipt failure');
  assert.equal(store.counts().quarantine, 0);
});

test('429 with Retry-After uses the server delay', async () => {
  const client = makeClient(() => ({ status: 429, ok: false, body: null, retryAfterMs: 3_600_000 }));
  const { forwarder, store } = makeForwarder({ client });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 1 && store.counts().processing === 0));
  await forwarder.stop();
  const reclaimed = store.claimNext();
  assert.equal(reclaimed, null);
});

test('429 without Retry-After falls back to backoff', async () => {
  const client = makeClient(() => ({ status: 429, ok: false, body: null, retryAfterMs: null }));
  const { forwarder, store } = makeForwarder({ client, backoff: () => 5_000 });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 1 && store.counts().processing === 0));
  await forwarder.stop();
  const [name] = readdirSync(join(root, 'retry')).filter((n) => n.startsWith('msg-'));
  const record = JSON.parse(readFileSync(join(root, 'retry', name), 'utf8'));
  assert.ok(record.attempts >= 1, 'attempt counted');
  assert.ok(record.availableAt - Date.now() < 5_000, 'scheduled with backoff, not a 1h Retry-After');
  const reclaimed = store.claimNext({ now: Date.now() + 5_100 });
  assert.equal(reclaimed.message.key.id, 'A1');
  assert.ok(reclaimed.attempts >= 1);
});

test('400 and 422 quarantine the record', async () => {
  for (const status of [400, 422]) {
    rmSync(root, { recursive: true, force: true });
    const client = makeClient(() => ({ status, ok: false, body: null, retryAfterMs: null }));
    const { forwarder, store } = makeForwarder({ client });
    store.ingest({ type: 'notify', messages: [msg('A1')] });
    forwarder.start();
    assert.ok(await waitFor(() => store.counts().quarantine === 1));
    await forwarder.stop();
    assert.equal(client.calls.length, 1, 'rejected payloads are never retried');
  }
});

test('401 pauses the world and the record returns to the ready queue', async () => {
  const client = makeClient(() => ({ status: 401, ok: false, body: null, retryAfterMs: null }));
  const { forwarder, store } = makeForwarder({ client });
  store.ingest({ type: 'notify', messages: [msg('A1'), msg('B1', '628222000002@s.whatsapp.net')] });
  forwarder.start();
  assert.ok(await waitFor(() => forwarder.paused));
  const callsAtPause = client.calls.length;
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(client.calls.length, callsAtPause, 'no further deliveries while paused');
  assert.equal(store.counts().retry, 2, 'paused work stays ready');
  await forwarder.stop();
  forwarder.resume();
  client.handler = () => ({ status: 200, ok: true, body: { results: [{ status: 'stored' }] }, retryAfterMs: null });
  const second = new InboundForwarder({ store, client, logger, pollMs: 2, backoff: () => 0 });
  activeForwarders.push(second);
  second.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0));
  await second.stop();
});

test('5xx and network errors retry with backoff', async () => {
  for (const result of [{ status: 502, ok: false, retryAfterMs: null }, { status: null, ok: false, error: 'network', retryAfterMs: null }]) {
    rmSync(root, { recursive: true, force: true });
    let calls = 0;
    const client = {
      async postEvents() {
        calls += 1;
        return calls === 1 ? result : { status: 200, ok: true, body: { results: [{ status: 'stored' }] }, retryAfterMs: null };
      },
    };
    const { forwarder, store } = makeForwarder({ client });
    store.ingest({ type: 'notify', messages: [msg('A1')] });
    forwarder.start();
    assert.ok(await waitFor(() => calls >= 2 && store.counts().retry === 0 && store.counts().processing === 0));
    await forwarder.stop();
  }
});

test('workers run up to maxJids conversations concurrently', async () => {
  let inFlight = 0;
  let peak = 0;
  const client = {
    async postEvents() {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 40));
      inFlight -= 1;
      return { status: 200, ok: true, body: { results: [{ status: 'stored' }] }, retryAfterMs: null };
    },
  };
  const { forwarder, store } = makeForwarder({ client });
  for (let i = 1; i <= 7; i++) {
    store.ingest({ type: 'notify', messages: [msg(`C${i}`, `62811100000${i}@s.whatsapp.net`)] });
  }
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0), 'all delivered');
  await forwarder.stop();
  assert.ok(peak >= 2, `concurrent deliveries happened (peak ${peak})`);
  assert.ok(peak <= 5, `never more than 5 conversations in flight (peak ${peak})`);
});

test('per-JID order is preserved under concurrency', async () => {
  const seen = {};
  const client = {
    async postEvents(payload) {
      const id = payload.messages[0].key.id;
      const jid = payload.messages[0].key.remoteJid;
      seen[jid] = seen[jid] ?? [];
      seen[jid].push(id);
      await new Promise((r) => setTimeout(r, 5));
      return { status: 200, ok: true, body: { results: [{ status: 'stored' }] }, retryAfterMs: null };
    },
  };
  const { forwarder, store } = makeForwarder({ client });
  for (let i = 1; i <= 6; i++) {
    store.ingest({ type: 'notify', messages: [msg(`A${i}`, '628111000001@s.whatsapp.net'), msg(`B${i}`, '628222000002@s.whatsapp.net')] });
  }
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0));
  await forwarder.stop();
  assert.deepEqual(seen['628111000001@s.whatsapp.net'], ['A1', 'A2', 'A3', 'A4', 'A5', 'A6']);
  assert.deepEqual(seen['628222000002@s.whatsapp.net'], ['B1', 'B2', 'B3', 'B4', 'B5', 'B6']);
});
