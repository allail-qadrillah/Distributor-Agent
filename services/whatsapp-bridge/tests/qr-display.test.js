import test from 'node:test';
import assert from 'node:assert/strict';
import { displayQr } from '../src/qr-display.js';

test('local mode renders block art without exposing the QR string', () => {
  const chunks = [];
  const write = (s) => chunks.push(s);
  const result = displayQr('2@SECRETQRPAIRING123,secret==', { mode: 'local', write });
  const out = chunks.join('');
  assert.equal(result, 'displayed');
  assert.ok(/█|▀|▄/.test(out), 'renders half-block art');
  assert.ok(!out.includes('SECRETQRPAIRING'), 'raw QR string never printed as text');
});

test('docker mode returns a stop-container action and renders nothing', () => {
  const chunks = [];
  const result = displayQr('2@SECRETQRPAIRING123,secret==', { mode: 'docker', write: (s) => chunks.push(s) });
  assert.equal(result, 'stop-container');
  assert.equal(chunks.join(''), '');
});
