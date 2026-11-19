// Inbound durable queue (Section 9). Every payload is persisted at the
// socket-listener boundary with a synchronous durable write, then split
// into one record per message. Records move between directories only by
// atomic rename, so a crash at any point leaves a recoverable state:
//
//   incoming/   event-<hash>.json  raw payloads not yet fully split
//   retry/      msg-<id>.json      ready now (availableAt null) or
//                                  waiting for a retry time; while a
//                                  JID's earliest record waits here, no
//                                  later record of that JID is claimable
//                                  and the JID keeps its active slot
//   processing/ msg-<id>.json      claimed, delivery in flight
//   quarantine/ msg-<id>.json      dead letters (manual recovery), also
//                                  written directly at split time for
//                                  messages with no usable JID
//
// Startup recovery: processing -> retry, then re-split any leftover raw
// events (splitting is idempotent via full-message-key dedup). A raw
// event is deleted only after its split succeeds; persistence errors
// propagate so the event survives for the next recovery attempt.
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, renameSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { atomicWriteDurable, ensureDir } from './fsutil.js';

const MSG_PREFIX = 'msg-';
const EVENT_PREFIX = 'event-';

export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`;
}

// Dedup identity: sha256 of the full WAMessage key (remoteJid, id, fromMe,
// participant — everything present), so a redelivery of the same message
// from WhatsApp or a replayed raw event maps to the same file name.
export function keyRecordId(key) {
  return createHash('sha256').update(canonicalJson(key)).digest('hex');
}

function messageRecordId(message) {
  return createHash('sha256').update(canonicalJson(message)).digest('hex');
}

export class InboundStore {
  constructor({ dataDir, logger }) {
    this.incomingDir = join(dataDir, 'incoming');
    this.processingDir = join(dataDir, 'processing');
    this.retryDir = join(dataDir, 'retry');
    this.quarantineDir = join(dataDir, 'quarantine');
    for (const dir of [this.incomingDir, this.processingDir, this.retryDir, this.quarantineDir]) {
      ensureDir(dir);
    }
    this.logger = logger;
    this._seqBase = Date.now().toString(36);
    this._seqCounter = 0;
  }

  ingest(payload) {
    const hash = this.persistEvent(payload);
    const result = this.splitEvent(payload);
    this.deleteEvent(hash);
    return result;
  }

  persistEvent(payload) {
    const hash = createHash('sha256').update(canonicalJson(payload)).digest('hex');
    const path = this.eventPath(hash);
    if (!existsSync(path)) {
      atomicWriteDurable(path, JSON.stringify(payload));
    }
    return hash;
  }

  deleteEvent(hash) {
    try {
      unlinkSync(this.eventPath(hash));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }

  eventPath(hash) {
    return join(this.incomingDir, `${EVENT_PREFIX}${hash}.json`);
  }

  // Baileys (lib/Types/Message.d.ts): `notify` = message just received,
  // `append` = history-sync backfill, no notification. Only `notify` is
  // new input, so `append` is dropped.
  splitEvent(payload) {
    const type = payload?.type;
    const messages = Array.isArray(payload?.messages) ? payload.messages : [];
    if (type !== 'notify') {
      return { created: 0, skipped: 0, quarantined: 0, dropped: messages.length };
    }
    let created = 0;
    let skipped = 0;
    let quarantined = 0;
    let dropped = 0;
    for (const message of messages) {
      const key = message?.key;
      const usableJid =
        key !== null &&
        typeof key === 'object' &&
        typeof key.remoteJid === 'string' &&
        key.remoteJid !== '';
      if (!usableJid) {
        const id = messageRecordId(message);
        if (this._existsAnywhere(id)) {
          skipped += 1;
        } else {
          atomicWriteDurable(this.recordPath(this.quarantineDir, id), JSON.stringify({
            id,
            seq: this._nextSeq(),
            jid: null,
            key: key ?? null,
            message,
            type: 'notify',
            attempts: 0,
            availableAt: null,
            quarantineReason: 'no_usable_jid',
          }));
          quarantined += 1;
        }
        this.logger.warn({ event: 'inbound_message_without_usable_jid' });
        continue;
      }
      const id = keyRecordId(key);
      if (this._existsAnywhere(id)) {
        skipped += 1;
        continue;
      }
      const record = {
        id,
        seq: this._nextSeq(),
        jid: key.remoteJid,
        key,
        message,
        type,
        attempts: 0,
        availableAt: null,
      };
      atomicWriteDurable(this.recordPath(this.retryDir, id), JSON.stringify(record));
      created += 1;
    }
    return { created, skipped, quarantined, dropped };
  }

  // Per-JID FIFO under a `maxJids` ceiling: only a JID's earliest record
  // moves, and a head waiting for retry blocks its JID and keeps its slot.
  claimNext({ now = Date.now(), maxJids = 5 } = {}) {
    const inFlight = this._recordsIn(this.processingDir);
    const retrying = this._recordsIn(this.retryDir);
    const activeJids = new Set(inFlight.map((r) => r.jid));
    const blockedJids = new Set(activeJids);
    const heads = new Map();
    for (const record of retrying) {
      const head = heads.get(record.jid);
      if (!head || record.seq < head.seq) heads.set(record.jid, record);
    }
    for (const [jid, head] of heads) {
      if (head.availableAt != null && head.availableAt > now) {
        blockedJids.add(jid);
        activeJids.add(jid);
      }
    }
    if (activeJids.size >= maxJids) return null;
    const ready = retrying.filter(
      (record) =>
        !blockedJids.has(record.jid) &&
        (record.availableAt == null || record.availableAt <= now),
    );
    if (ready.length === 0) return null;
    ready.sort((a, b) => (a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0));
    const next = ready[0];
    renameSync(this.recordPath(this.retryDir, next.id), this.recordPath(this.processingDir, next.id));
    return next;
  }

  markDone(id) {
    try {
      unlinkSync(this.recordPath(this.processingDir, id));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }

  markRetry(record, delayMs, now = Date.now()) {
    const updated = {
      ...record,
      attempts: (record.attempts ?? 0) + 1,
      availableAt: now + delayMs,
    };
    atomicWriteDurable(this.recordPath(this.processingDir, record.id), JSON.stringify(updated));
    renameSync(this.recordPath(this.processingDir, record.id), this.recordPath(this.retryDir, record.id));
    return updated;
  }

  markQuarantine(record, reason) {
    const updated = { ...record, quarantineReason: reason };
    atomicWriteDurable(this.recordPath(this.processingDir, record.id), JSON.stringify(updated));
    renameSync(this.recordPath(this.processingDir, record.id), this.recordPath(this.quarantineDir, record.id));
    return updated;
  }

  // The Python webhook dedups on external message id, so redelivery is safe.
  recoverProcessing() {
    let recovered = 0;
    for (const record of this._recordsIn(this.processingDir)) {
      const updated = { ...record, availableAt: null };
      atomicWriteDurable(this.recordPath(this.processingDir, record.id), JSON.stringify(updated));
      renameSync(this.recordPath(this.processingDir, record.id), this.recordPath(this.retryDir, record.id));
      recovered += 1;
    }
    return recovered;
  }

  resplitAll() {
    let created = 0;
    for (const name of readdirSync(this.incomingDir)) {
      if (!name.startsWith(EVENT_PREFIX)) continue;
      const path = join(this.incomingDir, name);
      let payload;
      try {
        payload = JSON.parse(readFileSync(path, 'utf8'));
      } catch (error) {
        // Only unparseable content is corrupt. A read failure is a
        // persistence error: propagate it so the raw event survives
        // for the next startup recovery.
        if (!(error instanceof SyntaxError)) throw error;
        this.logger.warn({ event: 'inbound_event_corrupt', file: name });
        try {
          unlinkSync(path);
        } catch (unlinkError) {
          if (unlinkError.code !== 'ENOENT') throw unlinkError;
        }
        continue;
      }
      // Delete the raw event only after every record is durable: a
      // persistence failure (ENOSPC, EACCES, EIO, ...) propagates and
      // leaves the event for the next recovery attempt.
      created += this.splitEvent(payload).created;
      try {
        unlinkSync(path);
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    }
    return created;
  }

  recordPath(dir, id) {
    return join(dir, `${MSG_PREFIX}${id}.json`);
  }

  counts() {
    const count = (dir) => readdirSync(dir).filter((n) => n.startsWith(MSG_PREFIX)).length;
    return {
      pendingEvents: readdirSync(this.incomingDir).filter((n) => n.startsWith(EVENT_PREFIX)).length,
      retry: count(this.retryDir),
      processing: count(this.processingDir),
      quarantine: count(this.quarantineDir),
    };
  }

  _existsAnywhere(id) {
    return [this.retryDir, this.processingDir, this.quarantineDir].some((dir) =>
      existsSync(this.recordPath(dir, id)),
    );
  }

  _nextSeq() {
    return `${this._seqBase}${(this._seqCounter++).toString(36).padStart(6, '0')}`;
  }

  _recordsIn(dir) {
    const records = [];
    for (const name of readdirSync(dir)) {
      if (!name.startsWith(MSG_PREFIX)) continue;
      const path = join(dir, name);
      try {
        records.push(JSON.parse(readFileSync(path, 'utf8')));
      } catch {
        this.logger.warn({ event: 'inbound_record_corrupt', file: name });
        try {
          renameSync(path, join(this.quarantineDir, name));
        } catch (error) {
          if (error.code !== 'ENOENT') throw error;
        }
      }
    }
    return records;
  }
}
