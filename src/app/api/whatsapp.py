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


def messaging_stack_singleton(request: Request | None = None) -> MessagingStack:
    global _stack
    if _stack is None:
        try:
            _stack = build_messaging_stack()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"service_misconfigured: {type(exc).__name__}",
            ) from exc
    return _stack


def reset_messaging_stack() -> None:
    global _stack
    _stack = None


@router.post("/events")
def inbound_events(request: Request, payload: dict) -> dict:
    stack = messaging_stack_singleton(request)
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
    stack = messaging_stack_singleton()
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
