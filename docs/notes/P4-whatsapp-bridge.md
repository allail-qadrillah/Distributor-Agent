# Engineering Note, P4: WhatsApp Baileys Bridge

Date: 2026-11-19 · Status: complete and verified (unit + DB integration + Node tests + Docker build). Live WhatsApp pairing and delivery are NOT tested: no phone in this environment.

## 1. Purpose

Implement the Node.js WhatsApp bridge (`services/whatsapp-bridge/`) per the approved Phase 4 spec. The bridge is transport only. No business rules, database, broker, or Redis on the Node side. All business logic stays in Python.

The Phase 3 wire contract (`docs/notes/P3-whatsapp-adapter.md` §3–§5) is unchanged, except one approved change (§2 below). Other limits kept: no Baileys fork or patch, no QR/session/queue admin API, no media, no Prometheus. The only runtime dependency is `@whiskeysockets/baileys`, pinned exactly 6.7.24.

## 2. The approved Python change (spec §5)

A duplicate message (same conversation, external message id already stored) is now answered with `mark_read` computed from the stored message status, so a redelivered unread message still earns a read receipt. No new migration, no new table. The change lives in `MessageRepository.get_by_external_id()`, `InboundResult.duplicate` (same conversation only), `conversation_service`, and `processor`. The agent never re-runs for a duplicate.

## 3. Implementation structure

- Node 26 ESM. Modules in `src/`: config, logger, fsutil, backoff, qr, qr-display, session-store, connection-manager, inbound-store, inbound-forwarder, fastapi-client, delivery-store, receipts, delivery-reporter, outbound, httpserver, capacity, index. A one-line summary per module is in `services/whatsapp-bridge/README.md`.
- The QR encoder is in-repo (`src/qr.js`, ISO/IEC 18004 byte mode, levels L/M) because the runtime dependency list is locked. The `qrcode` package is a devDependency used only as a test oracle. Its output is verified bit-for-bit against that oracle.
- Tests: `node --test "tests/*.test.js"`, 135 tests, no network and no WhatsApp. Includes end-to-end scenario tests: inbound, outbound, crash during split, SIGTERM, and capacity. Ten regression tests cover the two 2026-11-19 review waves: per-JID FIFO across retry waits and quarantine at split (first wave), and raw-event preservation when a startup split fails (second wave).
- Baileys is imported only by the modules that need it. The connection manager takes an injected `makeSocket` factory, so tests can fake the socket.

## 4. Key implementation decisions

Full rationale is in `docs/DECISIONS.md` (2026-11-19 entry). Operational detail is in `services/whatsapp-bridge/README.md`. In short:

- Inbound durability: a synchronous atomic write (tmp→fsync→rename→dir fsync) at the listener boundary. One record per message, keyed `sha256(full message key)`. Dedup checks the retry/processing/quarantine directories. The raw event file is deleted only after the split succeeds, so the capacity guard sees the drain and a persistence failure leaves the event for the next startup. Startup recovery: processing→retry, plus an idempotent re-split in which only unparseable content is `inbound_event_corrupt` (warned and dropped); every other error (a read failure, or a record write failing with ENOSPC/EACCES/EIO) propagates, so startup fails loudly and the raw event survives. A `notify` message with no usable `remoteJid` in its key is quarantined at split time (reason `no_usable_jid`, jid null, the full message preserved, id = sha256 of the full message), never claimable and never occupying a JID slot. Crash recovery preserves it.
- Forwarder retry policy: 429 → `Retry-After`. 400/422 → quarantine. Manual recovery: move the file back to `retry/` (a `no_usable_jid` record must be corrected first, see the next bullet). 401/403/404/405 → global pause until restart. No code path calls `resume`, so a pause truly lasts until restart. 5xx/timeout/network → exponential backoff capped at 5 minutes. The read receipt (`readMessages`) is sent before markDone. Claiming is per-JID FIFO: `claimNext` serves only a JID's earliest retry record, a head waiting for a retry time blocks its JID and keeps its slot (max 5), and one waiting JID never blocks the global queue.
- `no_usable_jid` quarantine recovery (second review wave, same working tree): the generic move-to-`retry/` applies to webhook-rejection records whose key is valid. It must not be applied to a `no_usable_jid` record: an uncorrected keyless message is delivered, Python answers 200 `skipped:invalid_event_shape`, and the bridge deletes the record as done, silently losing it. The operator procedure lives in `services/whatsapp-bridge/README.md` ("Quarantine recovery"): identify the record by `quarantineReason`, inspect the preserved `message`, establish the true key from a trusted source (never invented), correct `message.key` plus the record's own `key` and `jid`, verify `remoteJid`/`id`/`fromMe`, and only then move to `retry/`. Otherwise the record stays in quarantine.
- Outbound: the id is preallocated, then the mapping `outbound/<messageId>.json` is persisted BEFORE the send. Single-flight gate: at most 10 requests waiting, past that 429. Send timeout → 500 `unknown`, mapping kept. Confirmed failure → 200 `failed`, mapping deleted.
- Receipts: `fromMe` only. ERROR(0) → `failed`. 3/4/5 → `delivered`. 1/2/unrecognized → mapping held. Reports retry until 2xx. The mapping is deleted only after the ack.
- Capacity: 1 GB combined across the queue directories. 80% → suspend (a controlled disconnect, draining continues). 60% → reconnect. Hysteresis prevents flapping.
- QR never enters logs or disk. `local` mode renders to the terminal. `docker` mode: the bridge stops itself so pairing happens from a local run, then the container restarts.
- Fail-closed configuration: token, webhook secret, and FastAPI URL are required.

## 5. Docker

The root `Dockerfile` (python:3.12-slim, `uv sync --frozen`) runs the app. `services/whatsapp-bridge/Dockerfile` (node:26-slim, `npm ci --omit=dev`, non-root user, `/health` healthcheck) runs the bridge. `docker-compose.yml` uses a private network. Port 3001 is NOT published: the app reaches the bridge by service name. Stop grace period 30 seconds. The `./data/whatsapp-bridge` bind mount holds the session and the queues.

## 6. Verification (2026-11-19)

- `uv run pytest -q --ignore=tests/integration` → 576 passed.
- `RUN_DB_SMOKE=1 uv run pytest -q tests/integration` → 44 passed / 3 skipped (the +1 over the Phase 3 baseline is the seed-integrity test from `359ff16`. The §5 change added unit tests and extended one existing integration test).
- `cd services/whatsapp-bridge && npm test` → 131 passed (135 after the second review wave, below).
- `uv run ruff check src tests` → clean. Format drift in 15 files = the identical set as HEAD `976433c` (pre-existing, untouched).
- `docker compose config --quiet` + both image builds OK. `app.main` imports OK. `src/index.js` imports in a node:26 container.
- Second review wave (2026-11-19, R1 startup-recovery data loss + R2 `no_usable_jid` recovery docs): the R1 regression tests failed RED first ("Missing expected exception: the persistence error must surface" on all three), then green after the fix. `node --test tests/inbound-store.test.js` → 26 passed. `cd services/whatsapp-bridge && npm test` → 135 passed. `uv run pytest -q --ignore=tests/integration` → 576 passed. `uv run ruff check src tests` → clean. `docker compose config --quiet` → OK. DB smoke and image builds not re-run (no Python, Docker, or schema change).
- The first operational run needs local-mode pairing against `data/whatsapp-bridge/`, then a restart in compose.
