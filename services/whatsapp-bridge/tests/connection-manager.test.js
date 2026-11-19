import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { DisconnectReason } from '@whiskeysockets/baileys';
import { ConnectionManager, classifyDisconnect } from '../src/connection-manager.js';

function makeFactory() {
  const sockets = [];
  const factory = (authState) => {
    const socket = new EventEmitter();
    socket.authState = authState;
    socket.ended = false;
    socket.end = () => { socket.ended = true; };
    socket.ev = socket;
    sockets.push(socket);
    return socket;
  };
  return { sockets, factory };
}

function makeStore(saved = null) {
  const store = {
    saved,
    saveCount: 0,
    cleared: false,
    load() { return this.saved ? { creds: this.saved, keys: null } : null; },
    saveCreds(creds) { this.saveCount += 1; this.saved = creds; },
    keyStore() { return { get: async () => ({}), set: async () => {} }; },
    clear() { this.cleared = true; this.saved = null; },
  };
  return store;
}

function makeLogger() {
  const capture = (level) => (event, fields = {}) => ({ level, event, ...fields });
  const logger = {
    lines: [],
    info: (event, fields) => { const l = capture('info')(event, fields); logger.lines.push(l); },
    warn: (event, fields) => { const l = capture('warn')(event, fields); logger.lines.push(l); },
    error: (event, fields) => { const l = capture('error')(event, fields); logger.lines.push(l); },
  };
  return logger;
}

const close = (reason) => ({
  connection: 'close',
  lastDisconnect: { error: { output: { statusCode: reason } } },
});
const tick = (ms = 25) => new Promise((resolve) => setTimeout(resolve, ms));

function makeManager(overrides = {}) {
  const { sockets, factory } = makeFactory();
  const store = overrides.store ?? makeStore();
  const logger = makeLogger();
  const manager = new ConnectionManager({
    makeSocket: overrides.makeSocket ?? factory,
    store,
    logger,
    backoff: overrides.backoff ?? (() => 5),
    ...overrides.options,
  });
  return { manager, sockets, store, logger, factory };
}

test('classifyDisconnect buckets every reason correctly', () => {
  const c = (code) => classifyDisconnect({ error: { output: { statusCode: code } } });
  assert.equal(c(DisconnectReason.loggedOut).action, 're-pair');
  assert.equal(c(DisconnectReason.forbidden).action, 're-pair');
  assert.equal(c(DisconnectReason.multideviceMismatch).action, 're-pair');
  assert.equal(c(DisconnectReason.badSession).action, 're-pair');
  assert.equal(c(DisconnectReason.connectionReplaced).action, 'stop');
  assert.equal(c(DisconnectReason.connectionClosed).action, 'reconnect');
  assert.equal(c(DisconnectReason.timedOut).action, 'reconnect');
  assert.equal(c(DisconnectReason.unavailableService).action, 'reconnect');
  assert.equal(c(DisconnectReason.restartRequired).action, 'reconnect');
  assert.equal(c(undefined).action, 'reconnect');
});

test('start uses saved credentials when a session exists', () => {
  const creds = { me: { id: '628111@s.whatsapp.net' }, registered: true };
  const { manager, sockets } = makeManager({ store: makeStore(creds) });
  manager.start();
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].authState.creds, creds);
  assert.ok(sockets[0].authState.keys.get);
});

test('start with no session creates fresh credentials', () => {
  const { manager, sockets } = makeManager();
  manager.start();
  assert.equal(sockets.length, 1);
  assert.ok(sockets[0].authState.creds, 'fresh creds object exists');
  assert.ok(!sockets[0].authState.creds.registered);
});

test('open resets backoff, close schedules reconnect and closes the old socket', async () => {
  const attempts = [];
  const { manager, sockets, logger } = makeManager({
    backoff: (attempt) => { attempts.push(attempt); return 5; },
  });
  manager.start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  assert.equal(manager.connected, true);
  sockets[0].ev.emit('connection.update', close(DisconnectReason.connectionClosed));
  assert.equal(sockets[0].ended, true, 'old socket ended');
  assert.equal(sockets[0].listenerCount('connection.update'), 0, 'old listeners removed');
  await tick();
  assert.equal(sockets.length, 2, 'reconnected once');
  sockets[1].ev.emit('connection.update', { connection: 'open' });
  sockets[1].ev.emit('connection.update', close(DisconnectReason.timedOut));
  await tick();
  assert.deepEqual(attempts, [0, 0], 'attempt counter reset after each open');
  assert.ok(logger.lines.some((l) => l.event === 'wa_reconnect_scheduled'));
});

test('loggedOut clears the session and stops reconnecting', async () => {
  const { manager, sockets, store } = makeManager();
  manager.start();
  sockets[0].ev.emit('connection.update', close(DisconnectReason.loggedOut));
  await tick();
  assert.equal(store.cleared, true);
  assert.equal(sockets.length, 1, 'no reconnect after logout');
  assert.equal(manager.stopped, true);
});

test('connectionReplaced stops reconnecting but keeps the session', async () => {
  const { manager, sockets, store } = makeManager();
  manager.start();
  sockets[0].ev.emit('connection.update', close(DisconnectReason.connectionReplaced));
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(store.cleared, false);
  assert.equal(manager.stopped, true);
});

test('qr updates reach onQr and never reach the logger', () => {
  const seen = [];
  const { manager, sockets, logger } = makeManager({
    options: { onQr: (qr) => seen.push(qr) },
  });
  manager.start();
  sockets[0].ev.emit('connection.update', { qr: '2@SECRETQRPAIRING,xyz==' });
  assert.deepEqual(seen, ['2@SECRETQRPAIRING,xyz==']);
  const flat = JSON.stringify(logger.lines);
  assert.ok(!flat.includes('SECRETQRPAIRING'));
});

test('messages.upsert and messages.update are forwarded', () => {
  const messages = [];
  const updates = [];
  const { manager, sockets } = makeManager({
    options: {
      onMessages: (u) => messages.push(u),
      onMessagesUpdate: (u) => updates.push(u),
    },
  });
  manager.start();
  sockets[0].ev.emit('messages.upsert', { messages: [{ key: { id: 'A' } }], type: 'notify' });
  sockets[0].ev.emit('messages.update', [{ key: { id: 'A' }, update: { status: 3 } }]);
  assert.equal(messages.length, 1);
  assert.equal(updates.length, 1);
});

test('creds.update persists the current credentials', () => {
  const { manager, sockets, store } = makeManager();
  manager.start();
  sockets[0].ev.emit('creds.update');
  assert.equal(store.saveCount, 1);
  assert.equal(store.saved, manager.creds);
});

test('stop() ends the socket and cancels pending reconnects', async () => {
  const { manager, sockets } = makeManager();
  manager.start();
  sockets[0].ev.emit('connection.update', close(DisconnectReason.connectionClosed));
  manager.stop();
  assert.equal(sockets[0].ended, true);
  await tick();
  assert.equal(sockets.length, 1, 'cancelled reconnect never creates a socket');
});

test('start() is idempotent', () => {
  const { manager, sockets } = makeManager();
  manager.start();
  manager.start();
  assert.equal(sockets.length, 1);
});

test('suspend ends the socket and suppresses reconnects until resume', async () => {
  const { manager, sockets } = makeManager();
  manager.start();
  sockets[0].ev.emit('connection.update', { connection: 'open' });
  manager.suspend();
  assert.equal(sockets[0].ended, true);
  sockets[0].ev.emit('connection.update', close(DisconnectReason.connectionClosed));
  await tick();
  assert.equal(sockets.length, 1, 'suspended: no reconnect after close');
  manager.resume();
  await tick();
  assert.equal(sockets.length, 2, 'resumed: reconnects');
  assert.equal(manager.connected, false);
});

test('stop() wins over resume()', async () => {
  const { manager, sockets } = makeManager();
  manager.start();
  manager.suspend();
  manager.stop();
  manager.resume();
  await tick();
  assert.equal(sockets.length, 1);
});
