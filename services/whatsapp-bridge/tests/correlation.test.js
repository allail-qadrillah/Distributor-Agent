// Cross-service tracing (X-Correlation-ID): the bridge generates one
// correlation id per inbound delivery flow, forwards it to the FastAPI
// webhook and delivery-report endpoints as a header, and carries it on
// the outbound path (POST /messages -> mapping -> delivery report) so a
// trace spans Python -> bridge -> WhatsApp -> Python.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig, ConfigError } from '../src/config.js';
import { FastApiClient } from '../src/fastapi-client.js';
import { InboundStore } from '../src/inbound-store.js';
import { InboundForwarder } from '../src/inbound-forwarder.js';
import { DeliveryStore } from '../src/delivery-store.js';
import { OutboundSender } from '../src/outbound.js';
import { DeliveryReporter } from '../src/delivery-reporter.js';
import { createHttpServer } from '../src/httpserver.js';

const logger = { info() {}, warn() {}, error() {} };
const capturing = () => {
  const events = [];
  return {
    events,
    info: (event, fields) => events.push({ level: 'info', event, fields }),
    warn: (event, fields) => events.push({ level: 'warn', event, fields }),
    error: (event, fields) => events.push({ level: 'error', event, fields }),
  };
};

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-corr-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const waitFor = async (fn, timeoutMs = 2000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return fn();
};

const BASE = {
  WHATSAPP_BRIDGE_TOKEN: 'tok-123',
  WHATSAPP_WEBHOOK_SECRET: 'sec-456',
  WHATSAPP_BRIDGE_FASTAPI_URL: 'http://127.0.0.1:8000',
};

// -- config ------------------------------------------------------------------

test('config defaults log level to info', () => {
  assert.equal(loadConfig(BASE).logLevel, 'info');
});

test('config applies the LOG_LEVEL override (case-insensitive)', () => {
  assert.equal(loadConfig({ ...BASE, LOG_LEVEL: 'debug' }).logLevel, 'debug');
  assert.equal(loadConfig({ ...BASE, LOG_LEVEL: 'WARN' }).logLevel, 'warn');
});

test('config rejects an invalid LOG_LEVEL (fail closed)', () => {
  assert.throws(() => loadConfig({ ...BASE, LOG_LEVEL: 'loud' }), ConfigError);
});

// -- FastAPI client -----------------------------------------------------------

function fakeResponse(status, body = {}) {
  return { status, headers: { get: () => null }, json: async () => body };
}

test('postEvents sends the X-Correlation-ID header when given', async () => {
  const calls = [];
  const client = new FastApiClient({
    baseUrl: 'http://fastapi:8000',
    webhookSecret: 's',
    fetchImpl: async (url, init) => { calls.push(init); return fakeResponse(200); },
  });
  await client.postEvents({ type: 'notify' }, { correlationId: 'corr-api-1' });
  assert.equal(calls[0].headers['x-correlation-id'], 'corr-api-1');
});

test('postEvents omits the header when no correlation id', async () => {
  const calls = [];
  const client = new FastApiClient({
    baseUrl: 'http://fastapi:8000',
    webhookSecret: 's',
    fetchImpl: async (url, init) => { calls.push(init); return fakeResponse(200); },
  });
  await client.postEvents({ type: 'notify' });
  assert.ok(!('x-correlation-id' in calls[0].headers));
});

test('postDeliveryReport sends the X-Correlation-ID header when given', async () => {
  const calls = [];
  const client = new FastApiClient({
    baseUrl: 'http://fastapi:8000',
    webhookSecret: 's',
    fetchImpl: async (url, init) => { calls.push(init); return fakeResponse(200); },
  });
  await client.postDeliveryReport({ outbox_id: 'u', status: 'delivered' }, { correlationId: 'corr-api-2' });
  assert.equal(calls[0].headers['x-correlation-id'], 'corr-api-2');
});

// -- inbound delivery flow ----------------------------------------------------

const msg = (id, jid = '628111000001@s.whatsapp.net') => ({
  key: { remoteJid: jid, id, fromMe: false },
  message: { conversation: `pesan ${id}` },
  messageTimestamp: 1_700_000_000,
});

test('inbound delivery generates a correlation id and sends it as a header option', async () => {
  const seen = [];
  const log = capturing();
  const client = {
    async postEvents(payload, opts) {
      seen.push(opts?.correlationId);
      return { status: 200, ok: true, body: { results: [{ status: 'stored', mark_read: false }] }, retryAfterMs: null };
    },
  };
  const store = new InboundStore({ dataDir: root, logger });
  const forwarder = new InboundForwarder({ store, client, logger: log, pollMs: 2, backoff: () => 0 });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0));
  await forwarder.stop();

  assert.ok(seen[0], 'correlation id generated');
  assert.match(seen[0], /^[A-Za-z0-9_-]{8,64}$/);
  const delivered = log.events.find((e) => e.event === 'inbound_delivered');
  assert.ok(delivered, 'inbound_delivered logged');
  assert.equal(delivered.fields.correlation_id, seen[0]);
});

test('the correlation id survives a retry: one id per delivery flow', async () => {
  const seen = [];
  let calls = 0;
  const client = {
    async postEvents(payload, opts) {
      calls += 1;
      seen.push(opts?.correlationId);
      return calls === 1
        ? { status: 502, ok: false, body: null, retryAfterMs: null }
        : { status: 200, ok: true, body: { results: [{ status: 'stored', mark_read: false }] }, retryAfterMs: null };
    },
  };
  const store = new InboundStore({ dataDir: root, logger });
  const forwarder = new InboundForwarder({ store, client, logger, pollMs: 2, backoff: () => 0 });
  store.ingest({ type: 'notify', messages: [msg('A1')] });
  forwarder.start();
  assert.ok(await waitFor(() => store.counts().retry === 0 && store.counts().processing === 0));
  await forwarder.stop();

  assert.ok(calls >= 2, 'record retried');
  assert.ok(seen[1], 'retry also carried a correlation id');
  assert.equal(seen[1], seen[0], 'same flow, same id');
});

// -- outbound path ------------------------------------------------------------

const outboxPayload = { outbox_id: '0b2f6d3e-1111-2222-3333-444455556666', to: '+628111000001', text: 'halo' };

test('outbound stores the request correlation id in the mapping and logs outbox_id', async () => {
  const log = capturing();
  const store = new DeliveryStore({ dataDir: root, logger });
  const sender = new OutboundSender({
    store, logger: log, send: async () => {}, generateId: () => '3EB0CORR1',
  });
  const result = await sender.handle(outboxPayload, { correlationId: 'corr-out-1' });
  assert.equal(result.status, 200);
  assert.equal(result.body.status, 'sent');

  const mapping = store.lookup('3EB0CORR1');
  assert.equal(mapping.correlationId, 'corr-out-1');
  assert.equal(mapping.outboxId, outboxPayload.outbox_id);

  const sent = log.events.find((e) => e.event === 'outbound_sent');
  assert.ok(sent, 'outbound_sent logged');
  assert.equal(sent.fields.outbox_id, outboxPayload.outbox_id);
  assert.equal(sent.fields.correlation_id, 'corr-out-1');
  const flat = JSON.stringify(log.events);
  assert.ok(!flat.includes('halo'), 'message body never logged');
});

test('outbound works without a correlation id (legacy callers)', async () => {
  const store = new DeliveryStore({ dataDir: root, logger });
  const sender = new OutboundSender({
    store, logger, send: async () => {}, generateId: () => '3EB0CORR2',
  });
  const result = await sender.handle(outboxPayload);
  assert.equal(result.status, 200);
  assert.equal(store.lookup('3EB0CORR2').correlationId, undefined);
});

// -- delivery reports ---------------------------------------------------------

test('delivery reports carry the stored correlation id as a header option', async () => {
  const log = capturing();
  const store = new DeliveryStore({ dataDir: root, logger });
  store.save({
    messageId: '3EB0CORR3',
    outboxId: outboxPayload.outbox_id,
    to: '628111000001@s.whatsapp.net',
    correlationId: 'corr-rep-1',
  });
  store.setStatus('3EB0CORR3', 'delivered');

  const reports = [];
  const client = {
    async postDeliveryReport(report, opts) {
      reports.push({ report, opts });
      return { ok: true, status: 200 };
    },
  };
  const reporter = new DeliveryReporter({ store, client, logger: log, pollMs: 2, backoff: () => 0 });
  reporter.start();
  assert.ok(await waitFor(() => reports.length >= 1 && store.lookup('3EB0CORR3') === null));
  await reporter.stop();

  assert.equal(reports[0].opts.correlationId, 'corr-rep-1');
  assert.equal(reports[0].report.outbox_id, outboxPayload.outbox_id);
  const reported = log.events.find((e) => e.event === 'delivery_reported');
  assert.ok(reported, 'delivery_reported logged');
  assert.equal(reported.fields.outbox_id, outboxPayload.outbox_id);
  assert.equal(reported.fields.correlation_id, 'corr-rep-1');
});

// -- http server --------------------------------------------------------------

function startServer({ token = 'bridge-token', outbound } = {}) {
  const server = createHttpServer({
    token,
    outbound: outbound ?? { handle: async () => ({ status: 200, body: { status: 'sent' } }) },
    isReady: () => true,
    logger: { info() {}, warn() {}, error() {} },
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, base: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

test('POST /messages forwards the X-Correlation-ID header to the outbound sender', async () => {
  const handled = [];
  const outbound = {
    handle: async (payload, opts) => {
      handled.push({ payload, opts });
      return { status: 200, body: { status: 'sent', message_id: '3EB0X' } };
    },
  };
  const { server, base } = await startServer({ outbound });
  try {
    const response = await fetch(`${base}/messages`, {
      method: 'POST',
      headers: {
        authorization: 'Bearer bridge-token',
        'content-type': 'application/json',
        'x-correlation-id': 'corr-http-1',
      },
      body: JSON.stringify(outboxPayload),
    });
    assert.equal(response.status, 200);
  } finally {
    server.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(handled.length, 1);
  assert.equal(handled[0].opts.correlationId, 'corr-http-1');
  assert.equal(handled[0].payload.outbox_id, outboxPayload.outbox_id);
});

test('POST /messages without the header passes no correlation id', async () => {
  const handled = [];
  const outbound = {
    handle: async (payload, opts) => {
      handled.push({ payload, opts });
      return { status: 200, body: { status: 'sent' } };
    },
  };
  const { server, base } = await startServer({ outbound });
  try {
    const response = await fetch(`${base}/messages`, {
      method: 'POST',
      headers: { authorization: 'Bearer bridge-token', 'content-type': 'application/json' },
      body: JSON.stringify(outboxPayload),
    });
    assert.equal(response.status, 200);
  } finally {
    server.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(handled.length, 1);
  assert.ok(!('correlationId' in handled[0].opts) || handled[0].opts.correlationId === undefined);
});

test('POST /messages drops a garbage X-Correlation-ID instead of forwarding it', async () => {
  const handled = [];
  const outbound = {
    handle: async (payload, opts) => {
      handled.push({ payload, opts });
      return { status: 200, body: { status: 'sent' } };
    },
  };
  const { server, base } = await startServer({ outbound });
  try {
    const response = await fetch(`${base}/messages`, {
      method: 'POST',
      headers: {
        authorization: 'Bearer bridge-token',
        'content-type': 'application/json',
        'x-correlation-id': 'bad value; DROP TABLE logs',
      },
      body: JSON.stringify(outboxPayload),
    });
    assert.equal(response.status, 200);
  } finally {
    server.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(handled.length, 1);
  assert.equal(handled[0].opts.correlationId, undefined);
});
