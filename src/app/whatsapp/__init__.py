"""WhatsApp adapter boundary (the Python half).

The transport itself is an EXTERNAL Baileys (Node.js) bridge service:
this package never runs WhatsApp protocol code, it defines and speaks
the bridge contract:

- inbound: the bridge forwards raw Baileys `messages.upsert` events to
  POST /webhooks/whatsapp/events (app.api.whatsapp). normalization.py
  extracts what matters (external message id, sender phone, text) and
  drops everything else (own echoes, groups, broadcasts, non-text).
- outbound: WhatsAppAdapter (adapter.py) POSTs a send request to the
  bridge and maps the outcome to sent / failed / unknown.
- delivery: the bridge reports final delivery on
  POST /webhooks/whatsapp/delivery.

Adapter rules (transport boundary only): no business services, no
repositories, no intent decisions, no approval decisions, no
conversation state. Read receipts are opt-in per webhook response
(mark_read), because Baileys sends them only through an explicit
sock.readMessages call — that is the suppression mechanism.
"""
