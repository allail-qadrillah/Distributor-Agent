"""WhatsApp webhook routes: the bridge's entry points into the
application.

- POST /webhooks/whatsapp/events: raw Baileys messages.upsert payloads.
  Normalization extracts customer messages; the processor owns every
  decision. The response tells the bridge what happened per message and
  whether it may send a READ RECEIPT (mark_read false = suppress: the
  application allows receipts only for messages the agent processed;
  Baileys sends them solely via an explicit sock.readMessages call).
- POST /webhooks/whatsapp/delivery: final delivery reports for outbound
  messages; a 'delivered' report reconciles unknown outcomes and
  activates the order approvals bound to that message.

AUTHORIZATION REQUIREMENT (documented, not implemented): the project has
no authentication contract yet. Before these routes see anything beyond
the bridge network, add bridge authentication (shared secret or mTLS)
and operator auth on the conversation routes. The service boundary
(app.messaging.*) is isolated so an auth layer wraps it without changes
to the workflow logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.security import verify_webhook_secret
from app.messaging.wiring import MessagingStack, build_messaging_stack
from app.whatsapp.normalization import normalize_inbound_event
from app.whatsapp.schemas import (
    DeliveryReportAck,
    WhatsAppDeliveryReport,
    WhatsAppEventAck,
)

router = APIRouter(
    prefix="/webhooks/whatsapp",
    tags=["whatsapp"],
    dependencies=[Depends(verify_webhook_secret)],
)

_stack: MessagingStack | None = None


def get_messaging_stack(request: Request | None = None) -> MessagingStack:
    """Lazy singleton: /health and unrelated tests never build the stack.
    Configuration errors (agent or classifier credentials) surface here
    as 503, at the first webhook call, never mid-conversation."""
    global _stack
    if _stack is None:
        try:
            _stack = build_messaging_stack()
        except Exception as exc:  # Agent/IntentClassifier configuration errors
            raise HTTPException(
                status_code=503,
                detail=f"service_misconfigured: {type(exc).__name__}",
            ) from exc
    return _stack


def reset_messaging_stack() -> None:
    """Test seam: forget the cached stack so the next call rebuilds it."""
    global _stack
    _stack = None


@router.post("/events")
def inbound_events(request: Request, payload: dict) -> dict:
    """Process one raw messages.upsert event. Every contained message is
    reported: an outcome, or a skip with the exact reason."""
    stack = get_messaging_stack(request)
    acks: list[WhatsAppEventAck] = []
    for normalized in normalize_inbound_event(payload):
        if normalized.message is None:
            acks.append(WhatsAppEventAck(status=f"skipped:{normalized.skip_reason}"))
            continue
        outcome = stack.processor.process(normalized.message)
        acks.append(
            WhatsAppEventAck(
                external_message_id=normalized.message.external_message_id,
                status=outcome.status,
                mark_read=outcome.mark_read,
                replied=outcome.replied,
            )
        )
    return {"results": [ack.model_dump() for ack in acks]}


@router.post("/delivery", response_model=DeliveryReportAck)
def delivery_report(report: WhatsAppDeliveryReport) -> DeliveryReportAck:
    """Record the bridge's final delivery outcome for one outbound
    message. 'delivered' reconciles unknown states and activates the
    approvals bound to that delivery IN THE SAME TRANSACTION; an
    already-terminal row is a no-op (duplicate report). When approvals
    became active, messages that were deferred behind them are
    reprocessed now (M1: a fast confirmation is processed exactly once,
    only after its approval exists)."""
    stack = get_messaging_stack()
    reconciliation = stack.outbox.apply_delivery_report(
        report.outbox_id, delivered=report.status == "delivered"
    )
    if reconciliation.activated_any and reconciliation.row is not None:
        stack.processor.reprocess_deferred(reconciliation.row.conversation_id)
    if reconciliation.row is None:
        return DeliveryReportAck(outbox_id=report.outbox_id, status="already_final")
    return DeliveryReportAck(
        outbox_id=report.outbox_id, status=reconciliation.row.status
    )
