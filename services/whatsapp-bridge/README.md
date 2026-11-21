# whatsapp-bridge

Node.js WhatsApp transport for `distributor-agent`, built on Baileys
(`@whiskeysockets/baileys` 6.7.24, pinned). **Transport only** — every
business decision lives in the Python service. The bridge owns:

- the WhatsApp socket (connect, reconnect, pairing),
- durable inbound/outbound queues on the local filesystem,
- read receipts (`mark_read` from the webhook ack),
- delivery receipts for its own outbound messages.

It never touches a database, broker, or business rule.

## Running

```text
node src/index.js        # or: npm start
```

Required environment (fail closed at startup):

```text
WHATSAPP_BRIDGE_DATA_DIR    # root for auth/ and queue dirs (default data/whatsapp-bridge)
WHATSAPP_BRIDGE_TOKEN       # Bearer token the Python side presents on POST /messages
WHATSAPP_WEBHOOK_SECRET     # X-Webhook-Secret on calls to the FastAPI webhooks
WHATSAPP_BRIDGE_FASTAPI_URL # e.g. http://app:8000
WHATSAPP_BRIDGE_HOST=127.0.0.1 WHATSAPP_BRIDGE_PORT=3001
WHATSAPP_BRIDGE_QR_MODE     # local (terminal QR) | docker (no terminal: stops the container)
LOG_LEVEL=info              # fatal|error|warn|info|debug|trace (pino names)
```

Pairing: first run has no session; Baileys emits a QR, it is rendered in
the terminal (`local` mode) — scan it with the WhatsApp phone. In
`docker` mode there is no terminal, so the bridge logs
`qr_pairing_required` and stops itself; pair from a local run against the
same `data/whatsapp-bridge/` directory, then restart the container.

## HTTP surface (loopback/compose network only; port not published)

- `POST /messages` — `Authorization: Bearer <token>`, body
  `{ "outbox_id": "<uuid>", "to": "+628...", "text": "..." }`.
  One send in flight; up to 10 requests may wait; past that: 429.
  Responses: `200 {status:"sent", message_id}` |
  `200 {status:"failed", error}` (confirmed not sent — retryable) |
  `500 {status:"unknown"}` (send timed out — never blindly resent) |
  `429` (gate full) | `400/401`.
- `GET /health` — liveness, always 200.
- `GET /ready` — 200 only when the WhatsApp socket is open, the inbound
  forwarder is not paused, and shutdown has not begun.

## Inbound pipeline

1. socket `messages.upsert` → **synchronous durable write** of the raw
   event (`incoming/event-<hash>.json`, tmp→fsync→rename→dir-fsync),
   then split into one `msg-<sha256(full message key)>.json` record per
   message (full-key dedup across retry/processing/quarantine). A message
   with no usable `remoteJid` is written straight to `quarantine/`
   (`no_usable_jid`, payload preserved) instead of being dropped. The raw
   event file is deleted once every record is durable.
2. Records wait in `retry/` (immediately ready or `availableAt` in the
   future), are claimed into `processing/` under per-JID FIFO with at
   most 5 conversations active: only a JID's earliest record moves, and
   a JID whose head waits for retry blocks its later messages and keeps
   its slot. Records are then POSTed one message per request to
   `/webhooks/whatsapp/events` (150s timeout, `X-Webhook-Secret`).
3. Response policy: 2xx → done (and `readMessages` first when the ack
   says `mark_read`; a read-receipt failure retries the record); 429 →
   retry after `Retry-After` (or backoff); 400/422 → `quarantine/`
   (manual recovery: see "Quarantine recovery" below); 401/403/404/405 →
   global pause until restart; 5xx/timeout/network → backoff retry
   (1s→…→5min cap).
4. Crash recovery on start: `processing/` → `retry/`, leftover raw
   events re-split (idempotent). A raw event is deleted only after its
   split succeeds. If a record write fails (ENOSPC, EACCES, EIO, …),
   startup fails loudly and the raw event stays in `incoming/` for the
   next recovery attempt.

## Quarantine recovery

Records in `quarantine/` are dead letters. Recover them by hand.

Most quarantined records (for example `http_400` and `http_422` webhook
rejections) carry a valid message key. Inspect the record, and move the
`msg-<id>.json` file from `quarantine/` back to `retry/` once the
rejection cause is gone. The forwarder delivers it again, and Python
dedups the redelivery.

A `no_usable_jid` record is different. It was quarantined at split time
because its message has no usable WhatsApp key. `message.key` is
missing, is not an object, or has no usable `remoteJid`. Do not apply
the generic move-to-`retry/` procedure to such a record. An uncorrected
record is delivered, Python answers 200 with
`skipped:invalid_event_shape` in the results, and the bridge marks the
record done and deletes it. The message is then lost.

Recover a `no_usable_jid` record like this:

1. Identify the record by `"quarantineReason": "no_usable_jid"` in the
   `msg-<id>.json` file.
2. Inspect the original `message` payload preserved in the record.
3. Establish the correct WhatsApp message key from a trusted source,
   for example the WhatsApp application or the sender. Never invent or
   guess a key.
4. Correct `message.key` in the record. Set the record's own `key` and
   `jid` fields from the corrected key as well, so per-JID FIFO
   scheduling and read receipts keep working.
5. Verify the corrected key before you move anything. It needs a usable
   `remoteJid` (a non-empty `<phone>@s.whatsapp.net`), a non-empty
   `id`, and `fromMe: false`.
6. Move the corrected record to `retry/` only after the key passes that
   check.
7. If you cannot establish the correct key, keep the record in
   `quarantine/`. An uncorrected `no_usable_jid` record never goes to
   `retry/`.

## Outbound pipeline

Per request: preallocate a Baileys message id → **persist
`outbound/<messageId>.json` (messageId → outbox_id) before the send** →
`sendMessage`. `messages.update` receipts (only `fromMe`) map
ERROR→`failed`, DELIVERY_ACK/READ/PLAYED→`delivered`; the reporter POSTs
`/webhooks/whatsapp/delivery` and retries until 2xx, deleting the
mapping only after the ack. A send that times out keeps the mapping so a
late receipt can still reconcile; a confirmed-not-sent send deletes it.

## Capacity

1 GB combined across `incoming/ processing/ retry/ quarantine/` and
`outbound/`. At 80% the socket is suspended (controlled disconnect —
ingestion stops, draining continues); below 60% it reconnects.

## Layout

```text
src/config.js             env parsing, fail closed
src/logger.js             pino JSON logs + redaction (jid hashing), pino-pretty on a dev TTY
src/fsutil.js             durable atomic writes, dir fsync, dirSize
src/backoff.js            exponential backoff + jitter
src/qr.js                 in-repo QR encoder (ISO/IEC 18004, byte mode)
src/qr-display.js         terminal render / docker stop action
src/session-store.js      LocalFileSessionStore (auth/creds.json, keys.json)
src/connection-manager.js connect/classify/reconnect, suspend for capacity
src/inbound-store.js      durable queue: split, dedup, claim, states
src/inbound-forwarder.js  worker pool + retry policy table
src/fastapi-client.js     webhook client, 150s timeout, X-Webhook-Secret
src/delivery-store.js     outbound messageId → outbox_id mappings
src/receipts.js           messages.update classification
src/delivery-reporter.js  delivery reports, retry-until-2xx
src/outbound.js           single-flight gate, map-before-send
src/httpserver.js         /messages, /health, /ready, constant-time Bearer
src/capacity.js           1GB guard with hysteresis
src/index.js              wiring + graceful SIGTERM shutdown
```

## Logging and correlation

One JSON line per event on stdout (`ts`, `level`, `service:
whatsapp-bridge`, `event`, fields). Message bodies, phones, tokens,
secrets, QR payloads, and Baileys creds are redacted at the destination
stream — every line is scrubbed after serialization, so no call path can
bypass redaction. Interactive dev terminals get pino-pretty colors
(devDependency, never installed in production); production logs JSON.

Cross-service tracing: every inbound record gets one `correlationId`
(random UUID, stored on the record so retries reuse it) sent as
`X-Correlation-ID` to `/webhooks/whatsapp/events`. On the outbound path
the Python caller's `X-Correlation-ID` on `POST /messages` is persisted
in the delivery mapping and sent back on the delivery report, so one
trace id spans Python → bridge → WhatsApp → Python.

## Tests

```text
npm test     # node:test, 151 tests; no network, no WhatsApp
```

`qrcode` is a devDependency used only as an oracle for the in-repo QR
encoder (the approved runtime dependency list has no QR package). Tests
run on the local Node; production images use `node:26-slim`.

## Docker

`Dockerfile` (node:26-slim, `npm ci --omit=dev`, non-root `node` user,
`/health` healthcheck) and the root `docker-compose.yml` (private
network, port 3001 not published, 30s stop grace,
`./data/whatsapp-bridge` bind mount).
