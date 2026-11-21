import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { LocalFileSessionStore } from '../src/session-store.js';

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-session-'));
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

function fakeCreds(me = { id: '6281111111111@s.whatsapp.net' }) {
  return { me, registered: true, noise: Buffer.from('x').toString('base64') };
}

test('first load returns empty state (no session yet)', () => {
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const state = store.load();
  assert.equal(state, null);
  assert.ok(existsSync(join(root, 'auth')));
});

test('saveCreds persists credentials to disk', async () => {
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const creds = fakeCreds();
  store.saveCreds(creds);
  const raw = JSON.parse(readFileSync(join(root, 'auth', 'creds.json'), 'utf8'));
  assert.equal(raw.me.id, '6281111111111@s.whatsapp.net');
});

test('load recovers persisted credentials after restart', async () => {
  const first = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const creds = fakeCreds();
  first.saveCreds(creds);
  const second = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const state = second.load();
  assert.equal(state.creds.me.id, '6281111111111@s.whatsapp.net');
  assert.equal(state.creds.registered, true);
});

test('key writes persist and reload after restart', async () => {
  const dir = join(root, 'auth');
  const first = new LocalFileSessionStore({ authDir: dir });
  first.load();
  await first.saveKeys({ 'pre-key': { 10: { keyId: { id: 1 } } } });
  const second = new LocalFileSessionStore({ authDir: dir });
  const state = second.load();
  const fetched = await state.keys.get('pre-key', ['10']);
  assert.ok(fetched[10]);
  assert.deepEqual(fetched[10].keyId, { id: 1 });
});

test('keys.set persists immediately (in-memory map kept in sync)', async () => {
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const keys = store.keyStore();
  await keys.set({ session: { '628999:1': { chainKey: 'k' } } });
  const got = await keys.get('session', ['628999:1']);
  assert.equal(got['628999:1'].chainKey, 'k');
  const raw = JSON.parse(readFileSync(join(root, 'auth', 'keys.json'), 'utf8'));
  assert.ok(raw.session['628999:1']);
});

test('keys.get returns empty object for unknown ids', async () => {
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  const got = await store.keyStore().get('pre-key', ['42']);
  assert.deepEqual(got, {});
});

test('clear removes all session files for a fresh pairing', async () => {
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  store.load();
  store.saveCreds(fakeCreds());
  await store.saveKeys({ 'pre-key': { 1: {} } });
  store.clear();
  assert.equal(readdirSync(join(root, 'auth')).length, 0);
  const fresh = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  assert.equal(fresh.load(), null);
});

test('session files use restrictive permissions', () => {
  if (process.platform === 'win32') return;
  const store = new LocalFileSessionStore({ authDir: join(root, 'auth') });
  store.load();
  store.saveCreds(fakeCreds());
  assert.equal(statSync(join(root, 'auth', 'creds.json')).mode & 0o777, 0o600);
});
