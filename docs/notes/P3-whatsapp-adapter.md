# Engineering Note, P3: WhatsApp Bridge Contract

Date: 2026-11-18 · Status: the contract is implemented on the Python side. Real WhatsApp delivery is NOT tested (bridge implementation: `docs/notes/P4-whatsapp-bridge.md`).

## 1. Purpose

Define the wire contract between the Python app and the external WhatsApp bridge (a Node.js service built on Baileys). The Python side implements the contract in full (inbound normalization, outbound adapter, webhooks). The bridge side only follows it. This document is referenced by `.env.example`.

## 2. Verified Baileys facts

Verified against the WhiskeySockets/Baileys source (2026-11-18):

- The `messages.upsert` event carries `{"messages": WAMessage[], "type": "notify" | "append"}`. `"notify"` = a live message (processed). `"append"` = history-sync backfill, SKIPPED, never replayed to the agent.
- `WAMessage.key = {remoteJid, id, fromMe, participant?}`. A 1:1 chat uses `<digits>@s.whatsapp.net`. Groups use `@g.us`, broadcast `status@broadcast`, newsletter `@newsletter`, and LID `@lid` (cannot be mapped to E.164).
- The customer's text lives in `message.conversation` OR `message.extendedTextMessage.text`. Other content types (image, document) carry no processable text at this milestone.
- Read receipts are NOT automatic: Baileys sends them only through an explicit `sock.readMessages([key])` call. The app relies on this (see §3).

## 3. Inbound: `POST /webhooks/whatsapp/events`

The bridge forwards the raw `messages.upsert` payload (not normalized). The app processes and answers per message:

```json
{
  "results": [
    {
      "external_message_id": "3A",
      "status": "processed",
      "mark_read": true,
      "replied": true
    },
    { "external_message_id": null, "status": "skipped:history_backfill", "mark_read": false, "replied": false }
  ]
}
```

- `status`: `processed` | `duplicate` | `unknown_sender` | `identity_mismatch` (the number now belongs to a different customer: stop + handoff, the transcript belongs to the linked customer, the link is never overwritten) | `human_handling` | `handoff` | `deferred` (an order approval is still `pending_delivery`: the message waits and is reprocessed after activation) | `agent_error` | `skipped:<reason>` (backfill, own outgoing echo, group, broadcast, newsletter, LID, no text, invalid shape).
- `mark_read: false` = the app ORDERS the bridge to hold the read receipt: do not call `sock.readMessages` for this message. The app allows a receipt only after the agent actually processed the message. A message under handoff or from an unknown sender stays unread to the customer.
- `replied: true` only when a reply was sent (or sent with an `unknown` result).

Inbound idempotency is handled by the app through `UNIQUE (messages.external_message_id)`. The bridge may safely resend the same event.

## 4. Outbound: `POST {WHATSAPP_BRIDGE_URL}/messages`

App → bridge, once per outgoing message:

```json
POST /messages
Authorization: Bearer {WHATSAPP_BRIDGE_TOKEN}
{ "outbox_id": "<uuid>", "to": "+6281234567890", "text": "..." }
```

`outbox_id` is the reconciliation key: the bridge MUST use it in the delivery report (§5). WhatsApp itself has no receiver-side idempotency key.

A 2xx response from the bridge:

```json
{ "status": "sent", "error": null }
```

- `"sent"` may be answered ONLY after `sock.sendMessage` resolves (the message was accepted and encrypted for delivery). Device-level receipts (DELIVERY_ACK, READ) arrive LATER through the delivery webhook.
- `"failed"` + `error` = the bridge or WhatsApp refused the message. The app may retry within the attempt budget.

The adapter result mapping (the core outbox safety rule):

| Condition | App status | Auto-retried? |
|---|---|---|
| Response `"sent"` | `sent` | not needed |
| Response `"failed"` | `failed` | yes, ≤ 3 attempts |
| HTTP 4xx (the bridge rejected the request itself: auth, validation) | `failed` | yes |
| HTTP 5xx (the bridge failed while processing: the message MAY have reached WhatsApp) | `unknown` | NEVER |
| Connection never established (ConnectError) | `failed` | yes |
| Response lost after the request left (read timeout, reset, unparseable 2xx body) | `unknown` | NEVER |

`unknown` is never auto-resent: the message may have arrived, and a blind resend could duplicate it to the customer. The only resolution is reconciliation (§5).

The 4xx/5xx mapping is a contract. Only an explicit bridge guarantee that a message was not sent before a 5xx may move 5xx to `failed`. This contract carries no such guarantee.

## 5. Delivery report: `POST /webhooks/whatsapp/delivery`

The bridge reports the final result per outgoing message, whenever it arrives:

```json
X-Webhook-Secret: {WHATSAPP_WEBHOOK_SECRET}
{ "outbox_id": "<uuid>", "status": "delivered" | "failed", "error": null }
```

- `delivered` reconciles a `pending`/`sending`/`unknown` row to `sent`, and ACTIVATES the order approval bound to that summary message (`order_approvals.pending_delivery → active`) in the SAME transaction as the status transition (a crash cannot split them). Activation also triggers reprocessing of messages previously DEFERRED while the approval was still `pending_delivery` (a quick customer confirmation is processed exactly once, only after the approval exists).
- `failed` reconciles the row to `failed` (terminal: the adapter does not resend, and the operator decides).
- A row that is already terminal (`sent`/`failed`) answers `already_final`. A duplicate or late report is a no-op.

### 5a. The stale `sending` claim (the `claimed_at` lease)

Every dispatch claims a `sending` row and stamps `claimed_at` (a lease). A `sending` row whose lease expired (`OUTBOX_STALE_SENDING_SECONDS`, default 60 s) means a dispatcher died mid-send: the result CANNOT be determined, so the reconciliation sweep moves the row to `unknown`. Such a row is never auto-resent. Only a delivery report (or an operator) resolves it. The sweep runs automatically at the start of every dispatch.

## 6. Security (implemented, Phase 3 remediation)

- App → bridge Bearer token: `WHATSAPP_BRIDGE_TOKEN` (verified by the bridge). Production with `WHATSAPP_BRIDGE_URL` but no token is rejected at startup.
- Bridge → app webhooks (`/webhooks/whatsapp/*`): shared secret `WHATSAPP_WEBHOOK_SECRET` in the `X-Webhook-Secret` header, compared constant-time (`hmac.compare_digest`).
- Operator routes (`/conversations/*`, `/outbox/dispatch`): shared token `OPERATOR_API_TOKEN` in the `X-Operator-Token` header, also constant-time.
- Fail-closed: an empty secret under `APP_ENVIRONMENT=production` (the default) rejects EVERY request with 401. `APP_ENVIRONMENT=development` is a documented bypass (dev only).
- This is an authentication boundary, not an identity system: no per-operator accounts, replay protection, or rotation protocol yet. Upgrade path: mTLS or per-bridge HMAC with timestamps, plus real operator accounts.
- Operator routes wait on the per-phone lock with a bounded budget (10 s): if an agent flow is running, the route answers 503 `conversation busy` (nothing mutated) instead of blocking for the agent provider's 120 s read timeout.

## 7. Configuration

```
APP_ENVIRONMENT=production            # or development (documented bypass)
WHATSAPP_BRIDGE_URL=                  # empty = inbound only, replies wait in the outbox
WHATSAPP_BRIDGE_TOKEN=                # REQUIRED in production when the URL is set
WHATSAPP_BRIDGE_TIMEOUT_SECONDS=15.0
WHATSAPP_WEBHOOK_SECRET=              # X-Webhook-Secret, empty in production = 401 for everything
OPERATOR_API_TOKEN=                   # X-Operator-Token, empty in production = 401 for everything
OUTBOX_STALE_SENDING_SECONDS=60.0     # stale 'sending' claim lease
```

An empty URL means the `WhatsAppAdapter` is not built: `dispatch_pending` answers 0 and nothing is sent. A safe inbound-only mode.

The contract in §3–§5 above stays authoritative for Phase 4. The bridge implementation is documented separately in `docs/notes/P4-whatsapp-bridge.md`.
