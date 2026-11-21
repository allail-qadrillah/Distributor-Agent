import test from 'node:test';
import assert from 'node:assert/strict';
import { computeBackoffMs } from '../src/backoff.js';

test('inbound/table backoff doubles then caps at max', () => {
  const opts = { maxMs: 60_000 };
  assert.equal(computeBackoffMs(0, opts, () => 1), 1000);
  assert.equal(computeBackoffMs(1, opts, () => 1), 2000);
  assert.equal(computeBackoffMs(2, opts, () => 1), 4000);
  assert.equal(computeBackoffMs(3, opts, () => 1), 8000);
  assert.equal(computeBackoffMs(4, opts, () => 1), 16_000);
  assert.equal(computeBackoffMs(5, opts, () => 1), 32_000);
  assert.equal(computeBackoffMs(6, opts, () => 1), 60_000);
  assert.equal(computeBackoffMs(100, opts, () => 1), 60_000);
});

test('backoff respects a five-minute inbound cap', () => {
  const opts = { maxMs: 300_000 };
  assert.equal(computeBackoffMs(9, opts, () => 1), 300_000);
});

test('jitter stays within [50%, 100%] of the base delay', () => {
  const opts = { maxMs: 60_000 };
  assert.equal(computeBackoffMs(0, opts, () => 0), 500);
  assert.equal(computeBackoffMs(0, opts, () => 1), 1000);
  const v = computeBackoffMs(3, opts, () => 0.5);
  assert.ok(v >= 4000 && v <= 8000, `got ${v}`);
});

test('jittered values never exceed the cap', () => {
  assert.ok(computeBackoffMs(6, { maxMs: 60_000 }, () => 0) <= 60_000);
  assert.ok(computeBackoffMs(6, { maxMs: 60_000 }, () => 1) <= 60_000);
});
