import test from 'node:test';
import assert from 'node:assert/strict';
import { createLogger, jidHash } from '../src/logger.js';

function capture() {
  const lines = [];
  return { lines, write(line) { lines.push(JSON.parse(line)); } };
}

test('logger emits structured JSON with event, level, service, ts', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  log.info('inbound_forwarded', { queue_type: 'inbound', retry_count: 0 });
  const entry = sink.lines[0];
  assert.equal(entry.event, 'inbound_forwarded');
  assert.equal(entry.level, 'info');
  assert.equal(entry.service, 'whatsapp-bridge');
  assert.equal(entry.queue_type, 'inbound');
  assert.equal(entry.retry_count, 0);
  assert.ok(typeof entry.ts === 'string');
});

test('logger respects the configured level', () => {
  const sink = capture();
  const log = createLogger({ level: 'warn', write: sink.write });
  log.info('dropped_because_below_level', {});
  log.warn('kept_warn', {});
  assert.equal(sink.lines.length, 1);
  assert.equal(sink.lines[0].event, 'kept_warn');
});

test('logger defaults to info level', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  log.info('kept', {});
  assert.equal(sink.lines.length, 1);
});

test('logger redacts message content, phones, tokens, secrets', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  log.info('send_attempted', {
    to: '+6281234567890',
    text: 'harga Aqua berapa',
    phone: '+6281234567890',
    token: 'bearer-secret',
    secret: 'webhook-secret',
    qr: '2@QRPAIRINGDATA',
    body: 'order reply text',
    creds: '{"noiseKey":"x"}',
  });
  const flat = JSON.stringify(sink.lines[0]);
  assert.ok(!flat.includes('+6281234567890'));
  assert.ok(!flat.includes('harga'));
  assert.ok(!flat.includes('bearer-secret'));
  assert.ok(!flat.includes('webhook-secret'));
  assert.ok(!flat.includes('QRPAIRING'));
  assert.ok(flat.includes('[redacted]'));
});

test('logger stringifies errors without leaking', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  const err = new Error('connect ECONNREFUSED 127.0.0.1:8000');
  log.error('fastapi_unreachable', { error: err, to: '+62899' });
  const flat = JSON.stringify(sink.lines[0]);
  assert.ok(flat.includes('ECONNREFUSED'));
  assert.ok(!flat.includes('+62899'));
});

test('logger masks international phone numbers anywhere in a string', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  log.warn('send_retry', { note: 'call to +14155551234 failed twice', jid: '628111000001@s.whatsapp.net' });
  const flat = JSON.stringify(sink.lines[0]);
  assert.ok(!flat.includes('14155551234'));
  assert.ok(!flat.includes('628111000001'));
  assert.ok(flat.includes('[masked]'));
});

test('child logger merges bound fields into every line', () => {
  const sink = capture();
  const log = createLogger({ write: sink.write });
  const child = log.child({ correlation_id: 'bridge-flow-42' });
  child.info('inbound_delivered', { jid: 'hashed' });
  assert.equal(sink.lines[0].correlation_id, 'bridge-flow-42');
  assert.equal(sink.lines[0].jid, 'hashed');
});

test('jidHash never contains the raw jid', () => {
  const h = jidHash('6281234567890@s.whatsapp.net');
  assert.equal(typeof h, 'string');
  assert.ok(!h.includes('6281234567890'));
  assert.equal(jidHash('6281234567890@s.whatsapp.net'), h);
  assert.notEqual(jidHash('6281111111111@s.whatsapp.net'), h);
});
