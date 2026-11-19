// Inbound delivery worker pool (Section 9 retry policy). Up to maxJids
// workers run concurrently; the store's claimNext guarantees per-JID FIFO
// (no overtaking within a conversation) and the 5-conversation ceiling.
//
// Response policy table:
//   2xx                    -> done (delete record); if the ack says
//                             mark_read, send the read receipt first
//   429 + Retry-After      -> retry after the server's delay
//   429 (no header)        -> retry with backoff
//   400 / 422              -> quarantine (payload rejected; manual fix)
//   401 / 403 / 404 / 405  -> global pause (bridge misconfigured against
//                             the API; retries would never succeed)
//   any other status,
//   timeout, network error -> retry with backoff (5 min cap)
import { computeBackoffMs } from './backoff.js';
import { jidHash } from './logger.js';

const QUARANTINE_CODES = new Set([400, 422]);
const PAUSE_CODES = new Set([401, 403, 404, 405]);

export class InboundForwarder {
  constructor({
    store,
    client,
    logger,
    markRead = null,
    maxJids = 5,
    pollMs = 500,
    backoff = computeBackoffMs,
    random = Math.random,
    maxBackoffMs = 300_000,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  }) {
    this.store = store;
    this.client = client;
    this.logger = logger;
    this.markRead = markRead;
    this.maxJids = maxJids;
    this.pollMs = pollMs;
    this.backoff = backoff;
    this.random = random;
    this.maxBackoffMs = maxBackoffMs;
    this.sleep = sleep;
    this.running = false;
    this._paused = false;
    this._workers = [];
    // yield to the event loop between deliveries so timers and I/O get a
    // turn even when records are always claimable (retry delay 0)
    this._yield = () => new Promise((resolve) => setImmediate(resolve));
  }

  get paused() {
    return this._paused;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._workers = Array.from({ length: this.maxJids }, () => this._workerLoop());
  }

  async stop() {
    this.running = false;
    await Promise.allSettled(this._workers);
    this._workers = [];
  }

  resume() {
    this._paused = false;
  }

  async _workerLoop() {
    while (this.running) {
      if (this._paused) {
        await this.sleep(this.pollMs);
        continue;
      }
      let record = null;
      try {
        record = this.store.claimNext({ maxJids: this.maxJids });
      } catch (error) {
        this.logger.error('inbound_store_error', { error: String(error) });
        await this.sleep(this.pollMs);
        continue;
      }
      if (!record) {
        await this.sleep(this.pollMs);
        continue;
      }
      try {
        await this._deliver(record);
      } catch (error) {
        this.logger.error('inbound_deliver_error', { error: String(error) });
        try {
          this.store.markRetry(record, this.backoff(record.attempts ?? 0, { maxMs: this.maxBackoffMs }, this.random));
        } catch { /* record stays in processing; recovered on restart */ }
      }
      await this._yield();
    }
  }

  async _deliver(record) {
    const payload = { type: record.type, messages: [record.message] };
    let result;
    try {
      result = await this.client.postEvents(payload);
    } catch (error) {
      result = { status: null, ok: false, body: null, retryAfterMs: null, error: String(error) };
    }

    if (result.ok) {
      try {
        if (result.body?.results?.[0]?.mark_read === true && this.markRead) {
          await this.markRead(record.key);
        }
      } catch (error) {
        // read receipt failed: put the record back so it is retried —
        // the Python side dedups the redelivery and re-answers mark_read
        const delay = this.backoff(record.attempts ?? 0, { maxMs: this.maxBackoffMs }, this.random);
        this.store.markRetry(record, delay);
        this.logger.warn('inbound_mark_read_failed', { jid: jidHash(record.jid), error: String(error) });
        return 'retry';
      }
      this.store.markDone(record.id);
      this.logger.info('inbound_delivered', { jid: jidHash(record.jid), attempts: record.attempts ?? 0 });
      return 'done';
    }

    const status = result.status;
    if (status != null && QUARANTINE_CODES.has(status)) {
      this.store.markQuarantine(record, `http_${status}`);
      this.logger.warn('inbound_quarantined', { jid: jidHash(record.jid), status });
      return 'quarantine';
    }
    if (status != null && PAUSE_CODES.has(status)) {
      this._paused = true;
      this.store.markRetry(record, 0);
      this.logger.error('inbound_paused', { status, hint: 'bridge cannot talk to the API; fix config and restart' });
      return 'paused';
    }
    const delay = status === 429 && result.retryAfterMs
      ? result.retryAfterMs
      : this.backoff(record.attempts ?? 0, { maxMs: this.maxBackoffMs }, this.random);
    this.store.markRetry(record, delay);
    this.logger.warn('inbound_retry_scheduled', {
      jid: jidHash(record.jid),
      status: status ?? result.error ?? 'unknown',
      delay_ms: delay,
    });
    return 'retry';
  }
}
