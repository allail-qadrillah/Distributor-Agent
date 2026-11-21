import { computeBackoffMs } from './backoff.js';
import { classifyReceipt } from './receipts.js';
import { jidHash } from './logger.js';

export class DeliveryReporter {
  constructor({
    store,
    client,
    logger,
    backoff = computeBackoffMs,
    random = Math.random,
    maxBackoffMs = 300_000,
    pollMs = 500,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  }) {
    this.store = store;
    this.client = client;
    this.logger = logger;
    this.backoff = backoff;
    this.random = random;
    this.maxBackoffMs = maxBackoffMs;
    this.pollMs = pollMs;
    this.sleep = sleep;
    this.running = false;
    this._loop = null;
    this._attempts = new Map(); // messageId -> failed attempt count
    this._nextTry = new Map(); // messageId -> earliest retry timestamp
  }

  // Feed a `messages.update` array straight from the socket.
  observe(updates) {
    if (!Array.isArray(updates)) return;
    for (const entry of updates) {
      const status = classifyReceipt(entry);
      if (!status) continue;
      const messageId = entry?.key?.id;
      if (!messageId) continue;
      const existing = this.store.lookup(messageId);
      if (!existing) continue; // receipt for a message we did not track
      if (existing.status === status) continue;
      this.store.setStatus(messageId, status);
      this.logger.info('receipt_observed', {
        jid: jidHash(entry.key.remoteJid ?? ''),
        status,
        outbox_id: existing.outboxId,
        correlation_id: existing.correlationId,
      });
    }
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._loop = this._run();
  }

  async stop() {
    this.running = false;
    if (this._loop) await this._loop;
    this._loop = null;
  }

  async _run() {
    while (this.running) {
      const now = Date.now();
      for (const record of this.store.pending()) {
        const nextTry = this._nextTry.get(record.messageId) ?? 0;
        if (nextTry > now) continue;
        await this._report(record);
      }
      await this.sleep(this.pollMs);
    }
  }

  async _report(record) {
    const report = {
      outbox_id: record.outboxId,
      status: record.status,
      reported_at: new Date().toISOString(),
    };
    let result;
    try {
      result = await this.client.postDeliveryReport(report, {
        correlationId: record.correlationId,
      });
    } catch (error) {
      result = { ok: false, status: null, error: String(error) };
    }
    if (result.ok) {
      this.store.remove(record.messageId);
      this._attempts.delete(record.messageId);
      this._nextTry.delete(record.messageId);
      this.logger.info('delivery_reported', {
        outbox_id: record.outboxId, status: record.status, correlation_id: record.correlationId,
      });
      return;
    }
    const attempt = (this._attempts.get(record.messageId) ?? 0) + 1;
    this._attempts.set(record.messageId, attempt);
    const delay = this.backoff(attempt - 1, { maxMs: this.maxBackoffMs }, this.random);
    this._nextTry.set(record.messageId, Date.now() + delay);
    this.logger.warn('delivery_report_retry_scheduled', {
      outbox_id: record.outboxId,
      status: result.status ?? result.error ?? 'unknown',
      delay_ms: delay,
      correlation_id: record.correlationId,
    });
  }
}
