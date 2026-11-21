import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { InboundStore, keyRecordId, canonicalJson } from '../src/inbound-store.js';

const logger = { lines: [], info() {}, warn(l) { this.lines.push(l); }, error() {} };

let root;
test.beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'wa-bridge-inbound-'));
  logger.lines.length = 0;
});
test.afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const store = () => new InboundStore({ dataDir: root, logger });

const msg = (id, jid = '6281111111111@s.whatsapp.net', overrides = {}) => ({
  key: { remoteJid: jid, id, fromMe: false },
  message: { conversation: 'halo' },
  messageTimestamp: 1_700_000_000,
  ...overrides,
});
const upsert = (...messages) => ({ type: 'notify', messages });

test('ingest splits an event into one durable record per message', () => {
  const s = store();
  const result = s.ingest(upsert(msg('AAA'), msg('BBB')));
  assert.deepEqual(result, { created: 2, skipped: 0, quarantined: 0, dropped: 0 });
  assert.equal(s.counts().retry, 2);
  assert.equal(s.counts().pendingEvents, 0, 'event file removed after full split');
  const record = JSON.parse(readFileSync(join(root, 'retry', `msg-${keyRecordId({ remoteJid: '6281111111111@s.whatsapp.net', id: 'AAA', fromMe: false })}.json`), 'utf8'));
  assert.equal(record.jid, '6281111111111@s.whatsapp.net');
  assert.equal(record.message.key.id, 'AAA');
  assert.equal(record.attempts, 0);
  assert.equal(record.availableAt, null);
});

test('redelivered events dedup on the full message key', () => {
  const s = store();
  s.ingest(upsert(msg('AAA', '6281111111111@s.whatsapp.net', { key: { remoteJid: '6281111111111@s.whatsapp.net', id: 'AAA', fromMe: false, participant: '628999@s.whatsapp.net' } })));
  const second = s.ingest(upsert(msg('AAA', '6281111111111@s.whatsapp.net', { key: { participant: '628999@s.whatsapp.net', id: 'AAA', fromMe: false, remoteJid: '6281111111111@s.whatsapp.net' } })));
  assert.deepEqual(second, { created: 0, skipped: 1, quarantined: 0, dropped: 0 });
  assert.equal(s.counts().retry, 1);
});

test('keys differing only in participant are distinct records', () => {
  const s = store();
  s.ingest(upsert(msg('AAA', '6281111111111@s.whatsapp.net', { key: { remoteJid: '6281111111111@s.whatsapp.net', id: 'AAA', fromMe: false, participant: '628999@s.whatsapp.net' } })));
  s.ingest(upsert(msg('AAA', '6281111111111@s.whatsapp.net')));
  assert.equal(s.counts().retry, 2);
});

test('history-sync (append) events are dropped, not forwarded', () => {
  const s = store();
  const result = s.ingest({ type: 'append', messages: [msg('H1'), msg('H2')] });
  assert.deepEqual(result, { created: 0, skipped: 0, quarantined: 0, dropped: 2 });
  assert.equal(s.counts().retry, 0);
  assert.equal(s.counts().pendingEvents, 0);
});

test('a notify message without a usable JID is quarantined, not dropped', () => {
  const s = store();
  const orphan = { message: { conversation: 'orphan' } };
  const result = s.ingest(upsert(orphan));
  assert.deepEqual(result, { created: 0, skipped: 0, quarantined: 1, dropped: 0 });
  assert.ok(logger.lines.some((l) => l.event === 'inbound_message_without_usable_jid'));
  assert.equal(s.counts().quarantine, 1);
  const [name] = readdirSync(join(root, 'quarantine')).filter((n) => n.startsWith('msg-'));
  const record = JSON.parse(readFileSync(join(root, 'quarantine', name), 'utf8'));
  assert.equal(record.quarantineReason, 'no_usable_jid');
  assert.deepEqual(record.message, orphan, 'the original WAMessage payload is preserved');
  assert.equal(s.claimNext(), null, 'a quarantined record is never claimed');
});

test('claimNext serves records in global seq order', () => {
  const s = store();
  s.ingest(upsert(msg('A1', '628111000001@s.whatsapp.net')));
  s.ingest(upsert(msg('B1', '628222000002@s.whatsapp.net')));
  const first = s.claimNext();
  const second = s.claimNext();
  assert.equal(first.message.key.id, 'A1');
  assert.equal(second.message.key.id, 'B1');
  assert.equal(s.counts().processing, 2);
});

test('claimNext enforces per-JID FIFO: a busy JID is not served twice', () => {
  const s = store();
  s.ingest(upsert(msg('A1', '628111000001@s.whatsapp.net')));
  s.ingest(upsert(msg('B1', '628222000002@s.whatsapp.net')));
  s.ingest(upsert(msg('A2', '628111000001@s.whatsapp.net')));
  const first = s.claimNext();
  const second = s.claimNext();
  assert.equal(first.message.key.id, 'A1');
  assert.equal(second.message.key.id, 'B1');
  assert.equal(s.claimNext(), null, 'A2 blocked while A1 in flight');
});

test('claimNext enforces the five active-JID ceiling', () => {
  const s = store();
  for (let i = 1; i <= 6; i++) {
    s.ingest(upsert(msg(`J${i}`, `62811100000${i}@s.whatsapp.net`)));
  }
  for (let i = 0; i < 5; i++) s.claimNext();
  assert.equal(s.claimNext(), null);
  assert.equal(s.counts().processing, 5);
});

test('records with a future availableAt are not claimable', () => {
  const s = store();
  s.ingest(upsert(msg('A1')));
  const claimed = s.claimNext();
  s.markRetry(claimed, 60_000);
  assert.equal(s.claimNext(), null, 'still waiting');
  const due = s.claimNext({ now: Date.now() + 61_000 });
  assert.equal(due.message.key.id, 'A1');
});

test('markDone removes the record from disk', () => {
  const s = store();
  s.ingest(upsert(msg('A1')));
  const claimed = s.claimNext();
  s.markDone(claimed.id);
  assert.equal(s.counts().processing, 0);
  assert.equal(s.counts().retry, 0);
});

test('markRetry bumps attempts and schedules availability', () => {
  const s = store();
  s.ingest(upsert(msg('A1')));
  const claimed = s.claimNext();
  const updated = s.markRetry(claimed, 5_000);
  assert.equal(updated.attempts, 1);
  assert.ok(updated.availableAt > Date.now());
  assert.equal(s.counts().retry, 1);
  assert.equal(s.counts().processing, 0);
});

test('markQuarantine parks the record with a reason and blocks reprocessing', () => {
  const s = store();
  s.ingest(upsert(msg('A1')));
  const claimed = s.claimNext();
  s.markQuarantine(claimed, 'rejected_by_api');
  assert.equal(s.counts().quarantine, 1);
  const again = s.ingest(upsert(msg('A1')));
  assert.deepEqual(again, { created: 0, skipped: 1, quarantined: 0, dropped: 0 });
  assert.equal(s.counts().quarantine, 1);
  assert.equal(s.counts().retry, 0);
});

test('recoverProcessing returns in-flight records to the ready queue', () => {
  const first = store();
  first.ingest(upsert(msg('A1'), msg('A2', '628222000002@s.whatsapp.net')));
  const claimed = first.claimNext();
  const restarted = store();
  assert.equal(restarted.recoverProcessing(), 1);
  assert.equal(restarted.counts().retry, 2);
  const reclaimed = restarted.claimNext();
  assert.equal(reclaimed.message.key.id, 'A1');
});

test('resplitAll completes splits interrupted by a crash', () => {
  const s = store();
  const payload = upsert(msg('A1'), msg('B1', '628222000002@s.whatsapp.net'));
  s.persistEvent(payload);
  assert.equal(s.counts().pendingEvents, 1);
  const restarted = store();
  const created = restarted.resplitAll();
  assert.equal(created, 2);
  assert.equal(restarted.counts().pendingEvents, 0);
  assert.equal(restarted.counts().retry, 2);
  const claimed = restarted.claimNext();
  restarted.markQuarantine(claimed, 'rejected_by_api');
  const third = store();
  third.persistEvent(payload);
  assert.equal(third.resplitAll(), 0, 'A1 quarantined, B1 already ready');
  assert.equal(third.counts().quarantine, 1);
  assert.equal(third.counts().retry, 1);
});

// Inject a deterministic persistence failure: retry/ becomes a regular
// file, so every record write into it fails with ENOTDIR. This works
// regardless of the user the tests run as (no permission tricks).
const breakRetryWrites = (s) => {
  rmSync(s.retryDir, { recursive: true });
  writeFileSync(s.retryDir, 'not a directory');
};
const repairRetryWrites = (s) => {
  rmSync(s.retryDir);
  mkdirSync(s.retryDir);
};

test('a failed record write preserves the raw event and is not reported as corrupt', () => {
  const s = store();
  const hash = s.persistEvent(upsert(msg('A1')));
  breakRetryWrites(s);
  assert.throws(() => s.resplitAll(), (error) => error.code === 'ENOTDIR', 'the persistence error must surface');
  assert.ok(existsSync(s.eventPath(hash)), 'the raw event survives the failed split');
  assert.ok(!logger.lines.some((l) => l.event === 'inbound_event_corrupt'), 'a write failure is not corruption');
});

test('a later startup recovery completes the split and only then deletes the event', () => {
  const s = store();
  const hash = s.persistEvent(upsert(msg('A1')));
  breakRetryWrites(s);
  assert.throws(() => s.resplitAll(), (error) => error.code === 'ENOTDIR', 'the persistence error must surface');
  repairRetryWrites(s);
  assert.equal(s.resplitAll(), 1);
  assert.ok(!existsSync(s.eventPath(hash)), 'deleted only after the split succeeded');
  assert.equal(s.counts().retry, 1);
  assert.equal(s.counts().pendingEvents, 0);
});

test('a partial split preserves the raw event and recovery does not duplicate records', () => {
  const s = store();
  const bad = { message: { conversation: 'orphan' } };
  const payload = upsert(bad, msg('OK', '628222000002@s.whatsapp.net'));
  const hash = s.persistEvent(payload);
  breakRetryWrites(s);
  assert.throws(() => s.resplitAll(), (error) => error.code === 'ENOTDIR', 'the persistence error must surface');
  assert.ok(existsSync(s.eventPath(hash)), 'the raw event survives the partial split');
  assert.equal(readdirSync(join(root, 'quarantine')).filter((n) => n.startsWith('msg-')).length, 1, 'the first record was already written');
  repairRetryWrites(s);
  assert.equal(s.resplitAll(), 1, 'only the valid message is still missing');
  assert.equal(s.counts().retry, 1);
  assert.equal(s.counts().quarantine, 1, 'the already-written record is not duplicated');
  assert.ok(!existsSync(s.eventPath(hash)), 'deleted once every record is durable');
  const claimed = s.claimNext();
  assert.equal(claimed.message.key.id, 'OK');
  s.markDone(claimed.id);
  assert.equal(s.resplitAll(), 0, 'a pass with nothing pending is a no-op');
});

test('a corrupt raw event is still warned about and deleted', () => {
  const s = store();
  writeFileSync(join(root, 'incoming', 'event-0000corrupt.json'), '{not json');
  assert.equal(s.resplitAll(), 0);
  assert.ok(logger.lines.some((l) => l.event === 'inbound_event_corrupt'));
  assert.equal(s.counts().pendingEvents, 0, 'the unparseable event is dropped');
});

test('corrupt records are quarantined instead of crashing claimNext', () => {
  const s = store();
  s.ingest(upsert(msg('A1')));
  const claimed = s.claimNext();
  writeFileSync(join(root, 'retry', 'msg-deadbeef.json'), '{not json');
  void claimed;
  const next = s.claimNext();
  assert.equal(next, null, 'only the corrupt record was available');
  assert.equal(s.counts().quarantine, 1);
});

test('canonicalJson is key-order independent', () => {
  assert.equal(
    canonicalJson({ a: 1, b: { z: 1, y: 2 } }),
    canonicalJson({ b: { y: 2, z: 1 }, a: 1 }),
  );
});

test('a later message cannot overtake an earlier message waiting for retry', () => {
  const s = store();
  const jid = '628111000001@s.whatsapp.net';
  s.ingest(upsert(msg('A1', jid)));
  const a = s.claimNext();
  s.markRetry(a, 60_000);
  s.ingest(upsert(msg('B1', jid)));
  assert.equal(s.claimNext(), null, 'B1 must not pass A1 while A1 waits');
  const due = s.claimNext({ now: Date.now() + 61_000 });
  assert.equal(due.message.key.id, 'A1', 'A1 becomes eligible again and stays first');
  s.markDone(due.id);
  const b = s.claimNext();
  assert.equal(b.message.key.id, 'B1', 'B1 is served only after A1 completes');
});

test('a JID waiting for retry keeps occupying one of the five slots', () => {
  const s = store();
  for (let i = 1; i <= 5; i++) {
    s.ingest(upsert(msg(`J${i}`, `62811100000${i}@s.whatsapp.net`)));
  }
  const claimed = [];
  for (let i = 0; i < 5; i++) claimed.push(s.claimNext());
  s.markRetry(claimed[0], 60_000);
  s.ingest(upsert(msg('J6', '628111000006@s.whatsapp.net')));
  assert.equal(s.claimNext(), null, 'five JIDs active: four in flight plus one waiting for retry');
  s.markDone(claimed[1].id);
  const sixth = s.claimNext();
  assert.equal(sixth.message.key.id, 'J6', 'a freed slot admits the sixth JID');
});

test('a JID waiting for retry does not block another JID', () => {
  const s = store();
  s.ingest(upsert(msg('A1', '628111000001@s.whatsapp.net')));
  const a = s.claimNext();
  s.markRetry(a, 60_000);
  s.ingest(upsert(msg('C1', '628222000002@s.whatsapp.net')));
  const c = s.claimNext();
  assert.equal(c.message.key.id, 'C1', 'an unrelated JID still progresses');
});

test('restart recovery preserves per-JID order against a persisted retry', () => {
  const first = store();
  first.ingest(upsert(msg('A1', '628111000001@s.whatsapp.net')));
  first.markRetry(first.claimNext(), 60_000);
  first.ingest(upsert(msg('B1', '628111000001@s.whatsapp.net')));
  const restarted = store();
  restarted.recoverProcessing();
  assert.equal(restarted.claimNext(), null, 'B1 cannot overtake the persisted A1');
  const due = restarted.claimNext({ now: Date.now() + 61_000 });
  assert.equal(due.message.key.id, 'A1');
  restarted.markDone(due.id);
  assert.equal(restarted.claimNext().message.key.id, 'B1');
});

test('a mixed event quarantines the invalid message and keeps the valid one flowing', () => {
  const s = store();
  const bad = { message: { conversation: 'orphan' } };
  const result = s.ingest(upsert(bad, msg('OK', '628222000002@s.whatsapp.net')));
  assert.deepEqual(result, { created: 1, skipped: 0, quarantined: 1, dropped: 0 });
  assert.equal(s.counts().quarantine, 1);
  assert.equal(s.counts().retry, 1);
  const ok = s.claimNext();
  assert.equal(ok.message.key.id, 'OK', 'the valid JID is not blocked by the quarantined record');
  s.markDone(ok.id);
  assert.equal(s.counts().quarantine, 1, 'the invalid message stays quarantined');
});

test('crash during a mixed split loses neither the valid nor the invalid message', () => {
  const s = store();
  const bad = { message: { conversation: 'orphan' } };
  const payload = upsert(bad, msg('OK', '628222000002@s.whatsapp.net'));
  s.persistEvent(payload);
  // crash partway through the split: the valid record is durable, the
  // quarantined one is not, and the raw event is still on disk
  s.splitEvent(upsert(msg('OK', '628222000002@s.whatsapp.net')));
  assert.equal(s.counts().retry, 1);
  assert.equal(s.counts().pendingEvents, 1);
  const restarted = store();
  assert.equal(restarted.resplitAll(), 0, 'the valid record is not re-created');
  assert.equal(restarted.counts().pendingEvents, 0);
  assert.equal(restarted.counts().retry, 1);
  assert.equal(restarted.counts().quarantine, 1, 'the invalid message is preserved');
  restarted.persistEvent(payload);
  assert.equal(restarted.resplitAll(), 0, 'repeating recovery creates no duplicates');
  assert.equal(restarted.counts().retry, 1);
  assert.equal(restarted.counts().quarantine, 1);
  const ok = restarted.claimNext();
  assert.equal(ok.message.key.id, 'OK', 'the valid message flows');
});
