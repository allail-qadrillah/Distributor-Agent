import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync, mkdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { atomicWriteDurable, ensureDir, fsyncDir, dirSize } from '../src/fsutil.js';

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-fsutil-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

test('atomicWriteDurable writes content that survives rename', () => {
  const path = join(root, 'file.json');
  atomicWriteDurable(path, '{"a":1}');
  assert.equal(readFileSync(path, 'utf8'), '{"a":1}');
  assert.ok(!existsSync(join(root, 'file.json.tmp')));
});

test('atomicWriteDurable replaces existing file atomically', () => {
  const path = join(root, 'file.json');
  atomicWriteDurable(path, '{"a":1}');
  atomicWriteDurable(path, '{"a":2}');
  assert.equal(readFileSync(path, 'utf8'), '{"a":2}');
});

test('atomicWriteDurable creates restrictive file permissions', () => {
  const path = join(root, 'file.json');
  atomicWriteDurable(path, 'x');
  if (process.platform !== 'win32') {
    assert.equal(statSync(path).mode & 0o777, 0o600);
  }
});

test('atomicWriteDurable overwrites do not widen permissions', () => {
  const path = join(root, 'file.json');
  atomicWriteDurable(path, 'x');
  atomicWriteDurable(path, 'y');
  if (process.platform !== 'win32') {
    assert.equal(statSync(path).mode & 0o777, 0o600);
  }
});

test('ensureDir creates 0700 directories recursively', () => {
  const path = join(root, 'a', 'b', 'c');
  ensureDir(path);
  assert.ok(existsSync(path));
  if (process.platform !== 'win32') {
    assert.equal(statSync(join(root, 'a')).mode & 0o777, 0o700);
    assert.equal(statSync(join(root, 'a', 'b')).mode & 0o777, 0o700);
    assert.equal(statSync(path).mode & 0o777, 0o700);
  }
});

test('fsyncDir does not throw on a real directory', () => {
  fsyncDir(root);
});

test('dirSize sums files across subdirectories', () => {
  mkdirSync(join(root, 'x'), { recursive: true });
  writeFileSync(join(root, 'x', 'a'), Buffer.alloc(10));
  writeFileSync(join(root, 'b'), Buffer.alloc(5));
  assert.equal(dirSize(root), 15);
});

test('dirSize of missing directory is zero', () => {
  assert.equal(dirSize(join(root, 'nope')), 0);
});
