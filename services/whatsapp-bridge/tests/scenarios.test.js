import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startBridge } from '../src/index.js';
import { InboundStore } from '../src/inbound-store.js';

const quietLogger = { info() {}, warn() {}, error() {} };

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-scenario-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const waitFor = async (fn, timeoutMs = 4000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return fn();
};

const inboundMessage = (id, jid = '628111000001@s.whatsapp.net', text = 'berapa harga') => ({
  key: { remoteJid: jid, id, fromMe: false },
  message: { conversation: text },
  messageTimestamp: 1_700_000_000,
});

function makeFakeApi() {
  const state = { events: [], reports: [], eventsStatus: 200, reportsStatus: 200, markRead: false };
  const jsonResponse = (status, body) => ({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    json: async () => body,
  });
  const fetchImpl = async (url, init) => {
    const body = JSON.parse(init.body);
    if (url.endsWith('/webhooks/whatsapp/events')) {
      state.events.push({ url, headers: init.headers, body });
      return jsonResponse(state.eventsStatus, { results: [{ status: 'stored', mark_read: state.markRead }] });
    }
    if (url.endsWith('/webhooks/whatsapp/delivery')) {
      state.reports.push({ url, headers: init.headers, body });
      return jsonResponse(state.reportsStatus, { outbox_id: body.outbox_id, status: 'ack' });
    }
    throw new Error(`unexpected url: ${url}`);
  };
  return { state, fetchImpl };
}

function makeFakeSockets() {
  const sockets = [];
  const factory = (authState) => {
    const socket = new EventEmitter();
    socket.ev = socket;
    socket.authState = authState;
    socket.sent = [];
    socket.readReceipts = [];
    socket.sendMessage = async (jid, content, opts) => {
      socket.sent.push({ jid, content, opts });
      return { key: { remoteJid: jid, id: opts.messageId, fromMe: true } };
    };
    socket.readMessages = async (keys) => { socket.readReceipts.push(keys); };
    socket.end = () => { socket.ended = true; };
    sockets.push(socket);
    return socket;
  };
  return { sockets, factory };
}

const baseConfig = () => ({
  dataDir: root,
  bridgeToken: 'bridge-token',
  webhookSecret: 'webhook-secret',
  fastapiUrl: 'http://fastapi.test',
  host: '127.0.0.1',
  port: 0,
  qrMode: 'local',
  fastapiTimeoutMs: 150_000,
  maxActiveJids: 5,
  outboundWaitLimit: 10,
  queueMaxBytes: 1024 ** 3,
  queueHighRatio: 0.8,
  queueLowRatio: 0.6,
});

async function start({ config = baseConfig(), fetchImpl, factory, capacityIntervalMs = 10_000, exit } = {}) {
  const api = fetchImpl ? null : makeFakeApi();
  const sockets = factory ? null : makeFakeSockets();
  const exits = [];
  const bridge = await startBridge({
    config,
    fetchImpl: fetchImpl ?? api.fetchImpl,
    makeSocket: factory ?? sockets.factory,
    capacityIntervalMs,
    exit: exit ?? ((code) => exits.push(code)),
  });
  return { bridge, api: api ?? { state: null }, sockets: sockets?.sockets ?? null, exits };
}

test('inbound: upsert -> durable record -> webhook -> mark_read -> done', async () => {
  const { bridge, api, sockets } = await start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  api.state.markRead = true;

  sockets[0].ev.emit('messages.upsert', {
    type: 'notify',
    messages: [inboundMessage('SC1'), inboundMessage('SC2', '628222000002@s.whatsapp.net')],
  });

  assert.ok(await waitFor(() => api.state.events.length === 2), 'both messages forwarded');
  assert.ok(await waitFor(() => bridge.inboundStore.counts().retry === 0 && bridge.inboundStore.counts().processing === 0));
  assert.ok(await waitFor(() => sockets[0].readReceipts.length === 2), 'read receipts sent');

  const first = api.state.events.find((e) => e.body.messages[0].key.id === 'SC1');
  assert.equal(first.url, 'http://fastapi.test/webhooks/whatsapp/events');
  assert.equal(first.headers['x-webhook-secret'], 'webhook-secret');
  assert.equal(first.body.type, 'notify');
  assert.equal(first.body.messages[0].message.conversation, 'berapa harga');

  await bridge.stop();
  assert.equal(sockets[0].ended, true);
});

test('inbound crash: in-flight record is recovered and redelivered by the next process', async () => {
  // simulate a process that claimed a record and died mid-delivery:
  // the record file sits in processing/ with no one working on it
  const dead = new InboundStore({ dataDir: root, logger: quietLogger });
  dead.ingest({ type: 'notify', messages: [inboundMessage('CR1')] });
  const claimed = dead.claimNext();
  assert.ok(claimed, 'record left in processing at crash time');
  assert.equal(dead.counts().processing, 1);

  const second = await start();
  assert.ok(await waitFor(() => second.api.state.events.length === 1), 'record recovered and redelivered');
  assert.ok(await waitFor(() => second.bridge.inboundStore.counts().retry === 0));
  assert.ok(await waitFor(() => second.bridge.inboundStore.counts().processing === 0));
  const delivered = second.api.state.events.filter((e) => e.body.messages[0].key.id === 'CR1');
  assert.equal(delivered.length, 1);
  await second.bridge.stop();
});

test('inbound crash during split: leftover raw event is resplit on startup', async () => {
  // simulate a crash between persistEvent and splitEvent
  const store = new InboundStore({ dataDir: root, logger: quietLogger });
  store.persistEvent({ type: 'notify', messages: [inboundMessage('SP1')] });
  assert.equal(store.counts().pendingEvents, 1);

  const { bridge, api } = await start();
  assert.ok(await waitFor(() => api.state.events.length === 1));
  assert.ok(await waitFor(() => bridge.inboundStore.counts().pendingEvents === 0));
  assert.equal(api.state.events[0].body.messages[0].key.id, 'SP1');
  await bridge.stop();
});

test('outbound: POST /messages maps before send and reconciles the receipt', async () => {
  const { bridge, api, sockets } = await start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });

  const mappingSeenAtSend = [];
  const originalSend = sockets[0].sendMessage;
  sockets[0].sendMessage = async (jid, content, opts) => {
    const files = readdirSync(join(root, 'outbound')).filter((n) => n.endsWith('.json'));
    const mapping = files.length === 1 ? JSON.parse(readFileSync(join(root, 'outbound', files[0]), 'utf8')) : null;
    mappingSeenAtSend.push(mapping && mapping.outboxId);
    return originalSend(jid, content, opts);
  };

  const response = await fetch(`http://127.0.0.1:${bridge.port}/messages`, {
    method: 'POST',
    headers: { authorization: 'Bearer bridge-token', 'content-type': 'application/json' },
    body: JSON.stringify({ outbox_id: '9c1d2e3f-0000-4000-8000-000000000001', to: '+628111000001', text: 'terima kasih' }),
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, 'sent');
  assert.equal(sockets[0].sent.length, 1);
  assert.equal(sockets[0].sent[0].jid, '628111000001@s.whatsapp.net');
  assert.equal(sockets[0].sent[0].opts.messageId, body.message_id);
  assert.deepEqual(mappingSeenAtSend, ['9c1d2e3f-0000-4000-8000-000000000001'], 'mapping existed before the send');

  // WhatsApp confirms delivery -> receipt -> delivery report to Python
  sockets[0].ev.emit('messages.update', [
    { key: { remoteJid: '628111000001@s.whatsapp.net', id: body.message_id, fromMe: true }, update: { status: 3 } },
  ]);
  assert.ok(await waitFor(() => api.state.reports.length === 1));
  assert.equal(api.state.reports[0].body.outbox_id, '9c1d2e3f-0000-4000-8000-000000000001');
  assert.equal(api.state.reports[0].body.status, 'delivered');
  assert.ok(await waitFor(() => readdirSync(join(root, 'outbound')).length === 0), 'mapping deleted after ack');
  await bridge.stop();
});

test('outbound error receipt reports failed', async () => {
  const { bridge, api, sockets } = await start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  const response = await fetch(`http://127.0.0.1:${bridge.port}/messages`, {
    method: 'POST',
    headers: { authorization: 'Bearer bridge-token' },
    body: JSON.stringify({ outbox_id: '9c1d2e3f-0000-4000-8000-000000000002', to: '+628111000001', text: 'x' }),
  });
  const body = await response.json();
  sockets[0].ev.emit('messages.update', [
    { key: { remoteJid: '628111000001@s.whatsapp.net', id: body.message_id, fromMe: true }, update: { status: 0 } },
  ]);
  assert.ok(await waitFor(() => api.state.reports.length === 1));
  assert.equal(api.state.reports[0].body.status, 'failed');
  await bridge.stop();
});

test('outbound gate: one in flight, ten waiting, eleventh gets 429', async () => {
  const { bridge, sockets } = await start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const originalSend = sockets[0].sendMessage;
  sockets[0].sendMessage = async (...args) => { await gate; return originalSend(...args); };

  const post = (n) => fetch(`http://127.0.0.1:${bridge.port}/messages`, {
    method: 'POST',
    headers: { authorization: 'Bearer bridge-token' },
    body: JSON.stringify({ outbox_id: `u-${n}`, to: '+628111000001', text: 'x' }),
  });
  const inFlight = post(0);
  const waiting = Array.from({ length: 10 }, (_, i) => post(i + 1));
  await new Promise((r) => setTimeout(r, 50));
  const rejected = await post(99);
  assert.equal(rejected.status, 429);
  assert.equal((await rejected.json()).error, 'busy');

  release();
  const results = await Promise.all([inFlight, ...waiting]);
  assert.ok(results.every((r) => r.status === 200));
  await bridge.stop();
});

test('ready endpoint tracks socket and forwarder state', async () => {
  const { bridge, sockets } = await start();
  const notReady = await fetch(`http://127.0.0.1:${bridge.port}/ready`);
  assert.equal(notReady.status, 503);
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  const ready = await fetch(`http://127.0.0.1:${bridge.port}/ready`);
  assert.equal(ready.status, 200);
  const health = await fetch(`http://127.0.0.1:${bridge.port}/health`);
  assert.equal(health.status, 200);
  await bridge.stop();
});

test('capacity overflow suspends the socket, drain reconnects', async () => {
  // tiny cap so a single record trips it
  const config = { ...baseConfig(), queueMaxBytes: 1, queueHighRatio: 0.8, queueLowRatio: 0.6 };
  const { bridge, sockets, api } = await start({ config, capacityIntervalMs: 20 });
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  sockets[0].ev.emit('messages.upsert', { type: 'notify', messages: [inboundMessage('CAP1')] });
  assert.ok(await waitFor(() => sockets[0].ended === true), 'controlled disconnect after overflow');
  assert.ok(await waitFor(() => api.state.events.length === 1));
  assert.ok(await waitFor(() => sockets.length >= 2 && !sockets[sockets.length - 1].ended), 'socket reconnected after drain');
  await bridge.stop();
});

test('qr in docker mode stops the bridge process', async () => {
  const { bridge, sockets, exits } = await start({ config: { ...baseConfig(), qrMode: 'docker' } });
  sockets[0].ev.emit('connection.update', { qr: '2@QRPAIRINGSECRET' });
  assert.ok(await waitFor(() => exits.length === 1), 'stop-then-exit path ran');
  assert.equal(exits[0], 0);
  assert.equal(sockets[0].ended, true);
  await bridge.stop();
});
