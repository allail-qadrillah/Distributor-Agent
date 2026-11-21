import { DisconnectReason, initAuthCreds } from '@whiskeysockets/baileys';
import { computeBackoffMs } from './backoff.js';

// Session is dead and must be re-paired from a QR (cleared from disk).
const FATAL = new Set([
  DisconnectReason.loggedOut,
  DisconnectReason.forbidden,
  DisconnectReason.multideviceMismatch,
  DisconnectReason.badSession,
]);
// Another connection owns this number: stop, keep session on disk.
const STOP = new Set([DisconnectReason.connectionReplaced]);

export function classifyDisconnect(lastDisconnect) {
  const code = lastDisconnect?.error?.output?.statusCode;
  if (FATAL.has(code)) return { code, action: 're-pair' };
  if (STOP.has(code)) return { code, action: 'stop' };
  return { code, action: 'reconnect' };
}

export class ConnectionManager {
  constructor({
    makeSocket,
    store,
    logger,
    onQr,
    onMessages,
    onMessagesUpdate,
    onOpen,
    onClose,
    onStopped,
    backoff = computeBackoffMs,
    random = Math.random,
    schedule = (fn, ms) => setTimeout(fn, ms),
    maxBackoffMs = 60_000,
  }) {
    this.makeSocket = makeSocket;
    this.store = store;
    this.logger = logger;
    this.onQr = onQr;
    this.onMessages = onMessages;
    this.onMessagesUpdate = onMessagesUpdate;
    this.onOpen = onOpen;
    this.onClose = onClose;
    this.onStopped = onStopped;
    this.backoff = backoff;
    this.random = random;
    this.schedule = schedule;
    this.maxBackoffMs = maxBackoffMs;
    this.socket = null;
    this.creds = null;
    this.attempt = 0;
    this.started = false;
    this.stopped = false;
    this._connected = false;
    this._suspended = false;
    this._timer = null;
  }

  get connected() {
    return this._connected;
  }

  start() {
    if (this.started) return;
    this.started = true;
    const saved = this.store.load();
    this.creds = saved?.creds ?? initAuthCreds();
    this._connect();
  }

  suspend() {
    this._suspended = true;
    this._connected = false;
    this._cleanupSocket();
  }

  resume() {
    if (this.stopped || !this._suspended) return;
    this._suspended = false;
    this._connect();
  }

  _connect() {
    if (this.stopped || this._suspended) return;
    this._cleanupSocket();
    const socket = this.makeSocket({ creds: this.creds, keys: this.store.keyStore() });
    this.socket = socket;
    socket.ev.on('connection.update', (u) => this._onConnectionUpdate(u));
    socket.ev.on('creds.update', () => this.store.saveCreds(this.creds));
    if (this.onMessages) socket.ev.on('messages.upsert', this.onMessages);
    if (this.onMessagesUpdate) socket.ev.on('messages.update', this.onMessagesUpdate);
  }

  _onConnectionUpdate(update) {
    if (update.qr) this.onQr?.(update.qr);
    if (update.connection === 'open') {
      this._connected = true;
      this.attempt = 0;
      this.logger.info('wa_connected');
      this.onOpen?.();
    }
    if (update.connection === 'close') {
      this._connected = false;
      const cls = classifyDisconnect(update.lastDisconnect);
      this._cleanupSocket();
      this.onClose?.(cls);
      if (this.stopped || this._suspended) return;
      if (cls.action === 're-pair') {
        this.stopped = true;
        this.store.clear();
        this.logger.warn('wa_session_invalid', { code: cls.code });
        this.onStopped?.(cls);
      } else if (cls.action === 'stop') {
        this.stopped = true;
        this.logger.warn('wa_connection_replaced');
        this.onStopped?.(cls);
      } else {
        const delay = this.backoff(this.attempt++, { maxMs: this.maxBackoffMs }, this.random);
        this.logger.info('wa_reconnect_scheduled', { delay_ms: delay });
        this._timer = this.schedule(() => this._connect(), delay);
      }
    }
  }

  _cleanupSocket() {
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    try { socket.ev.removeAllListeners(); } catch { /* socket already dead */ }
    try { socket.end(undefined); } catch { /* socket already dead */ }
  }

  stop() {
    this.stopped = true;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    this._cleanupSocket();
  }
}
