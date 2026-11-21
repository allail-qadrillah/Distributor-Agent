import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig, ConfigError } from '../src/config.js';

const BASE = {
  WHATSAPP_BRIDGE_TOKEN: 'tok-123',
  WHATSAPP_WEBHOOK_SECRET: 'sec-456',
  WHATSAPP_BRIDGE_FASTAPI_URL: 'http://127.0.0.1:8000',
};

test('config accepts full environment', () => {
  const c = loadConfig({ ...BASE, WHATSAPP_BRIDGE_DATA_DIR: '/tmp/x' });
  assert.equal(c.dataDir, '/tmp/x');
  assert.equal(c.bridgeToken, 'tok-123');
  assert.equal(c.webhookSecret, 'sec-456');
  assert.equal(c.fastapiUrl, 'http://127.0.0.1:8000');
  assert.equal(c.host, '127.0.0.1');
  assert.equal(c.port, 3001);
  assert.equal(c.qrMode, 'local');
});

test('config applies overrides', () => {
  const c = loadConfig({
    ...BASE,
    WHATSAPP_BRIDGE_DATA_DIR: '/tmp/x',
    WHATSAPP_BRIDGE_HOST: '0.0.0.0',
    WHATSAPP_BRIDGE_PORT: '3101',
    WHATSAPP_BRIDGE_QR_MODE: 'docker',
  });
  assert.equal(c.host, '0.0.0.0');
  assert.equal(c.port, 3101);
  assert.equal(c.qrMode, 'docker');
});

test('config defaults data dir to repo data root', () => {
  const c = loadConfig(BASE);
  assert.ok(c.dataDir.endsWith('data/whatsapp-bridge'));
});

test('config fails closed without token', () => {
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_BRIDGE_TOKEN: '' }),
    ConfigError,
  );
});

test('config fails closed without webhook secret', () => {
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_WEBHOOK_SECRET: '' }),
    ConfigError,
  );
});

test('config fails closed without fastapi url', () => {
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_BRIDGE_FASTAPI_URL: '' }),
    ConfigError,
  );
});

test('config rejects invalid port', () => {
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_BRIDGE_PORT: 'nope' }),
    ConfigError,
  );
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_BRIDGE_PORT: '99999' }),
    ConfigError,
  );
});

test('config rejects invalid qr mode', () => {
  assert.throws(
    () => loadConfig({ ...BASE, WHATSAPP_BRIDGE_QR_MODE: 'api' }),
    ConfigError,
  );
});

test('config keeps spec-fixed runtime constants', () => {
  const c = loadConfig(BASE);
  assert.equal(c.fastapiTimeoutMs, 150_000);
  assert.equal(c.maxActiveJids, 5);
  assert.equal(c.outboundWaitLimit, 10);
  assert.equal(c.queueMaxBytes, 1024 ** 3);
  assert.equal(c.queueHighRatio, 0.8);
  assert.equal(c.queueLowRatio, 0.6);
});
