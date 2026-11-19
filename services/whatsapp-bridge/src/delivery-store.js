// Durable outbound receipt mapping (Section 10). The bridge preallocates
// a messageId, persists messageId -> outbox_id BEFORE the WhatsApp send,
// and keeps the mapping until the Python side has acknowledged the
// delivery report. Crash at any point: mappings on disk are the truth.
import { existsSync, readdirSync, readFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { atomicWriteDurable, ensureDir } from './fsutil.js';

export class DeliveryStore {
  constructor({ dataDir, logger }) {
    this.dir = join(dataDir, 'outbound');
    ensureDir(this.dir);
    this.logger = logger;
  }

  mappingPath(messageId) {
    return join(this.dir, `${messageId}.json`);
  }

  save({ messageId, outboxId, to }) {
    const record = {
      messageId,
      outboxId,
      to,
      status: null,
      createdAt: new Date().toISOString(),
    };
    atomicWriteDurable(this.mappingPath(messageId), JSON.stringify(record));
    return record;
  }

  lookup(messageId) {
    const path = this.mappingPath(messageId);
    if (!existsSync(path)) return null;
    try {
      return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
      this.logger.warn('outbound_mapping_corrupt', { messageId });
      return null;
    }
  }

  setStatus(messageId, status) {
    const record = this.lookup(messageId);
    if (!record) return null;
    const updated = { ...record, status };
    atomicWriteDurable(this.mappingPath(messageId), JSON.stringify(updated));
    return updated;
  }

  remove(messageId) {
    const path = this.mappingPath(messageId);
    if (existsSync(path)) {
      try { unlinkSync(path); } catch { /* concurrent removal */ }
    }
  }

  pending() {
    const out = [];
    for (const name of readdirSync(this.dir)) {
      if (!name.endsWith('.json')) continue;
      try {
        const record = JSON.parse(readFileSync(join(this.dir, name), 'utf8'));
        if (record.status === 'delivered' || record.status === 'failed') out.push(record);
      } catch { /* corrupt file: skip, logged on lookup */ }
    }
    return out;
  }

  counts() {
    let total = 0;
    let pending = 0;
    for (const record of this.all()) {
      total += 1;
      if (record.status === 'delivered' || record.status === 'failed') pending += 1;
    }
    return { mappings: total, pendingReports: pending };
  }

  all() {
    const out = [];
    for (const name of readdirSync(this.dir)) {
      if (!name.endsWith('.json')) continue;
      try {
        out.push(JSON.parse(readFileSync(join(this.dir, name), 'utf8')));
      } catch { /* skip corrupt */ }
    }
    return out;
  }
}
