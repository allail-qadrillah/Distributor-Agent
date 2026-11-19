import test from 'node:test';
import assert from 'node:assert/strict';
import { FastApiClient } from '../src/fastapi-client.js';

function fakeResponse(status, body = {}, headers = {}) {
  return {
    status,
    headers: { get: (name) => headers[name.toLowerCase()] ?? null },
    json: async () => body,
  };
}

test('postEvents posts the payload with the webhook secret header', async () => {
  const calls = [];
  const client = new FastApiClient({
    baseUrl: 'http://fastapi:8000/',
    webhookSecret: 'sekrit',
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return fakeResponse(200, { results: [{ status: 'stored' }] });
    },
  });
  const result = await client.postEvents({ type: 'notify', messages: [{ key: { id: 'A' } }] });
  assert.equal(result.ok, true);
  assert.equal(result.status, 200);
  assert.equal(calls[0].url, 'http://fastapi:8000/webhooks/whatsapp/events');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['x-webhook-secret'], 'sekrit');
  assert.equal(calls[0].init.headers['content-type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].init.body), { type: 'notify', messages: [{ key: { id: 'A' } }] });
});

test('postDeliveryReport posts to the delivery endpoint', async () => {
  const calls = [];
  const client = new FastApiClient({
    baseUrl: 'http://fastapi:8000',
    webhookSecret: 's',
    fetchImpl: async (url, init) => { calls.push({ url, init }); return fakeResponse(200, { outbox_id: 'x', status: 'sent' }); },
  });
  const result = await client.postDeliveryReport({ outbox_id: 'u', status: 'delivered' });
  assert.ok(result.ok);
  assert.equal(calls[0].url, 'http://fastapi:8000/webhooks/whatsapp/delivery');
  assert.deepEqual(JSON.parse(calls[0].init.body), { outbox_id: 'u', status: 'delivered' });
});

test('retry-after seconds convert to milliseconds', async () => {
  const client = new FastApiClient({
    baseUrl: 'http://x',
    webhookSecret: 's',
    fetchImpl: async () => fakeResponse(429, {}, { 'retry-after': '2' }),
  });
  const result = await client.postEvents({});
  assert.equal(result.retryAfterMs, 2000);
  assert.equal(result.ok, false);
});

test('timeout produces an explicit timeout result', async () => {
  const client = new FastApiClient({
    baseUrl: 'http://x',
    webhookSecret: 's',
    timeoutMs: 20,
    fetchImpl: (url, init) => new Promise((_, reject) => {
      init.signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      });
    }),
  });
  const result = await client.postEvents({});
  assert.equal(result.error, 'timeout');
  assert.equal(result.status, null);
});

test('connection refused produces a network result', async () => {
  const client = new FastApiClient({
    baseUrl: 'http://x',
    webhookSecret: 's',
    fetchImpl: async () => { throw new Error('ECONNREFUSED'); },
  });
  const result = await client.postEvents({});
  assert.equal(result.error, 'network');
});

test('non-JSON bodies are tolerated', async () => {
  const client = new FastApiClient({
    baseUrl: 'http://x',
    webhookSecret: 's',
    fetchImpl: async () => ({ status: 502, headers: { get: () => null }, json: async () => { throw new Error('bad'); } }),
  });
  const result = await client.postEvents({});
  assert.equal(result.ok, false);
  assert.equal(result.body, null);
});

test('constructor requires a baseUrl', () => {
  assert.throws(() => new FastApiClient({}), /baseUrl/);
});
