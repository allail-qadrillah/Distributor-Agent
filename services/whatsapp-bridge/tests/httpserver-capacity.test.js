import test from 'node:test';
import assert from 'node:assert/strict';
import { createHttpServer, tokenMatches } from '../src/httpserver.js';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CapacityGuard } from '../src/capacity.js';

const logger = { lines: [], info(l) { this.lines.push(l); }, warn(l) { this.lines.push(l); }, error(l) { this.lines.push(l); } };

function startServer({ token = 'bridge-token', outbound, isReady = () => true } = {}) {
  const server = createHttpServer({
    token,
    outbound: outbound ?? { handle: async () => ({ status: 200, body: { status: 'sent' } }) },
    isReady,
    logger: { info() {}, warn() {}, error() {} },
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, base: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

const post = (base, path, body, headers = {}) => fetch(base + path, {
  method: 'POST',
  headers: { 'content-type': 'application/json', ...headers },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('tokenMatches is constant-time and strict', () => {
  assert.equal(tokenMatches('bridge-token', 'bridge-token'), true);
  assert.equal(tokenMatches('wrong', 'bridge-token'), false);
  assert.equal(tokenMatches(null, 'bridge-token'), false);
  assert.equal(tokenMatches('bridge-token', ''), false);
  assert.equal(tokenMatches('bridge-token', undefined), false);
});

test('POST /messages requires a matching bearer token', async () => {
  const { server, base } = await startServer({ token: 'bridge-token' });
  const unauthorized = await post(base, '/messages', { outbox_id: 'x', to: '+628111000001', text: 'hi' });
  assert.equal(unauthorized.status, 401);
  assert.equal((await unauthorized.json()).status, 'failed');
  const ok = await post(base, '/messages', { outbox_id: 'x', to: '+628111000001', text: 'hi' }, { authorization: 'Bearer bridge-token' });
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).status, 'sent');
  server.close();
});

test('POST /messages forwards the payload to the outbound sender', async () => {
  const seen = [];
  const outbound = { handle: async (payload) => { seen.push(payload); return { status: 200, body: { status: 'sent', message_id: '3EB01' } }; } };
  const { server, base } = await startServer({ outbound });
  const response = await post(base, '/messages', { outbox_id: 'u1', to: '+628111000001', text: 'halo' }, { authorization: 'Bearer bridge-token' });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, 'sent');
  assert.equal(body.message_id, '3EB01');
  assert.deepEqual(seen[0], { outbox_id: 'u1', to: '+628111000001', text: 'halo' });
  server.close();
});

test('invalid JSON body answers 400 without reaching the sender', async () => {
  let handled = 0;
  const outbound = { handle: async () => { handled += 1; return { status: 200, body: {} }; } };
  const { server, base } = await startServer({ outbound });
  const response = await post(base, '/messages', '{not json', { authorization: 'Bearer bridge-token' });
  assert.equal(response.status, 400);
  assert.equal((await response.json()).error, 'invalid_json');
  assert.equal(handled, 0);
  server.close();
});

test('GET /health is always 200 and GET /ready reflects readiness', async () => {
  const { server, base } = await startServer({ isReady: () => false });
  const health = await fetch(base + '/health');
  assert.equal(health.status, 200);
  assert.equal((await health.json()).status, 'ok');
  const notReady = await fetch(base + '/ready');
  assert.equal(notReady.status, 503);
  server.close();

  const ready = await startServer({ isReady: () => true });
  const readyResponse = await fetch(ready.base + '/ready');
  assert.equal(readyResponse.status, 200);
  ready.server.close();
});

test('unknown paths answer 404', async () => {
  const { server, base } = await startServer();
  const response = await fetch(base + '/nope');
  assert.equal(response.status, 404);
  assert.equal((await response.json()).status, 'failed');
  server.close();
});

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-capacity-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const KB = 1024;

test('capacity guard trips at the high watermark and recovers at the low one', () => {
  const lines = [];
  const cap = new CapacityGuard({
    dirs: [join(root, 'queue')],
    maxBytes: 10 * KB,
    logger: { info: (e, f) => lines.push([e, f]), warn: () => {}, error: (e, f) => lines.push([e, f]) },
  });
  const queue = join(root, 'queue');
  const file = join(queue, 'a.json');
  mkdirSync(queue, { recursive: true });

  const setKb = (kb) => writeFileSync(file, 'x'.repeat(kb * KB));

  assert.equal(cap.check().action, 'none', 'empty queue');
  setKb(7);
  assert.equal(cap.check().action, 'none', 'below high watermark');
  setKb(8);
  assert.equal(cap.check().action, 'disconnect', 'at 80%');
  setKb(9);
  assert.equal(cap.check().action, 'none', 'between watermarks: hold');
  setKb(6);
  assert.equal(cap.check().action, 'reconnect', 'drained to 60%');
  assert.equal(cap.high, false);
  setKb(7);
  assert.equal(cap.check().action, 'none', 'recovered; stays quiet below high');
  setKb(9);
  assert.equal(cap.check().action, 'disconnect', 're-fires after another climb');
  assert.ok(lines.some(([event]) => event === 'queue_capacity_high'));
  assert.ok(lines.some(([event]) => event === 'queue_capacity_recovered'));
});

test('capacity guard sums across directories', () => {
  const a = join(root, 'a');
  const b = join(root, 'b');
  mkdirSync(a, { recursive: true });
  mkdirSync(b, { recursive: true });
  const cap = new CapacityGuard({
    dirs: [a, b],
    maxBytes: 4 * KB,
    logger: { info() {}, warn() {}, error() {} },
  });
  writeFileSync(join(a, 'x'), 'x'.repeat(2 * KB));
  writeFileSync(join(b, 'y'), 'y'.repeat(2 * KB));
  assert.equal(cap.check().action, 'disconnect', '2KB + 2KB of 4KB total');
});
