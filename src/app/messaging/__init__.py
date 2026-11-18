"""Application layer for WhatsApp conversations (Phase 3).

Components (target architecture):
- identity.IdentityResolver: phone -> registered customer, or None.
- conversation_service.ConversationService: one permanent conversation
  per phone, inbound idempotent claim, transcript persistence and
  reconstruction, outbox intent creation.
- handoff.HandoffManager: needs_human / human_active / active state
  transitions with their policies.
- approval.ApprovalManager: order approval lifecycle bound to one order
  and one delivery-confirmed summary message.
- outbox.OutboxProcessor: outbound delivery state machine and dispatch.
- processor.MessageProcessor: the end-to-end inbound workflow (gate,
  agent run, persistence, reply dispatch) with per-conversation
  serialization.
- wiring: composition from settings for the API layer.

Layering: this package orchestrates; business rules stay in
app.services, SQL stays in app.repositories, the provider wire format
stays in app.agents.providers / app.whatsapp.
"""
