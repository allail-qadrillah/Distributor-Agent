// Outbound sending (Section 11). One send in flight at a time (WhatsApp
// tolerates little parallelism from a single session); up to `waitLimit`
// requests may queue behind it, past that the gate answers 429 — which
// the Python side reads as a retryable failure.
//
// Order of operations per request: preallocate messageId -> persist the
// messageId/outbox_id mapping durably -> send. A crash between send and
// response still leaves the mapping for receipt reconciliation.
// Confirmed-not-sent errors delete the mapping (no receipt will come);
// a send timeout keeps it and answers 5xx ("unknown" on the Python side).
import { generateMessageID } from '@whiskeysockets/baileys';
import { jidHash } from './logger.js';

export function phoneToJid(to) {
  const digits = String(to ?? '').replace(/^\+/, '');
  if (!/^\d{6,15}$/.test(digits)) return null;
  return `${digits}@s.whatsapp.net`;
}

export function validateOutboundRequest(payload) {
  if (!payload || typeof payload !== 'object') return 'invalid_request';
  if (typeof payload.outbox_id !== 'string' || !payload.outbox_id.trim()) return 'invalid_outbox_id';
  if (phoneToJid(payload.to) == null) return 'invalid_to';
  if (typeof payload.text !== 'string' || !payload.text.trim()) return 'invalid_text';
  return null;
}

export class OutboundSender {
  constructor({
    store,
    logger,
    send, // async (jid, text, messageId) => void
    generateId = generateMessageID,
    waitLimit = 10,
    sendTimeoutMs = 15_000,
  }) {
    this.store = store;
    this.logger = logger;
    this.send = send;
    this.generateId = generateId;
    this.waitLimit = waitLimit;
    this.sendTimeoutMs = sendTimeoutMs;
    this.shuttingDown = false;
    this._active = false;
    this._queue = [];
  }

  get waiting() {
    return this._queue.length;
  }

  beginShutdown() {
    this.shuttingDown = true;
  }

  // Returns { status, body } — body always carries {status, error?}.
  async handle(payload) {
    const invalid = validateOutboundRequest(payload);
    if (invalid) {
      return { status: 400, body: { status: 'failed', error: invalid } };
    }
    if (this.shuttingDown) {
      return { status: 200, body: { status: 'failed', error: 'shutting_down' } };
    }
    let turn;
    try {
      turn = this._acquire();
    } catch {
      return { status: 429, body: { status: 'failed', error: 'busy' } };
    }
    if (turn) await turn; // our slot in the FIFO behind the current send
    try {
      return await this._sendOne(payload);
    } finally {
      this._release();
    }
  }

  async _sendOne({ outbox_id, to, text }) {
    const jid = phoneToJid(to);
    const messageId = this.generateId();
    this.store.save({ messageId, outboxId: outbox_id, to: jid });
    try {
      await this._withTimeout(this.send(jid, text, messageId));
      this.logger.info('outbound_sent', { jid: jidHash(jid), message_id: messageId });
      return { status: 200, body: { status: 'sent', message_id: messageId } };
    } catch (error) {
      if (error?.timeout) {
        this.logger.error('outbound_send_timeout', { jid: jidHash(jid), message_id: messageId });
        return { status: 500, body: { status: 'unknown', error: 'send_timeout' } };
      }
      this.store.remove(messageId);
      this.logger.warn('outbound_send_failed', { jid: jidHash(jid), error: String(error) });
      return { status: 200, body: { status: 'failed', error: 'send_failed' } };
    }
  }

  _acquire() {
    if (!this._active) {
      this._active = true;
      return null;
    }
    if (this._queue.length >= this.waitLimit) {
      throw new Error('gate_full');
    }
    return new Promise((resolve) => this._queue.push(resolve));
  }

  _release() {
    const next = this._queue.shift();
    if (next) next();
    else this._active = false;
  }

  _withTimeout(promise) {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error('send timeout');
        error.timeout = true;
        reject(error);
      }, this.sendTimeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }
}
