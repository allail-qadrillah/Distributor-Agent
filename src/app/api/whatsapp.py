import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.security import verify_webhook_secret
from app.messaging.wiring import MessagingStack, build_messaging_stack
from app.observability import log_event
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

_log = logging.getLogger(__name__)

_stack: MessagingStack | None = None


def messaging_stack_singleton(request: Request | None = None) -> MessagingStack:
    global _stack
    if _stack is None:
        try:
            _stack = build_messaging_stack()
        except Exception as exc:
            log_event(
                _log,
                "messaging_stack_misconfigured",
                level=logging.ERROR,
                error_type=type(exc).__name__,
                exc=exc,
            )
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
    normalized_all = normalize_inbound_event(payload)
    log_event(_log, "webhook_event_received", messages_found=len(normalized_all))
    acks: list[WhatsAppEventAck] = []
    for normalized in normalized_all:
        if normalized.message is None:
            log_event(
                _log,
                "webhook_message_skipped",
                level=logging.DEBUG,
                skip_reason=normalized.skip_reason,
            )
            acks.append(WhatsAppEventAck(status=f"skipped:{normalized.skip_reason}"))
            continue
        log_event(
            _log,
            "webhook_message_normalized",
            level=logging.DEBUG,
            external_message_id=normalized.message.external_message_id,
        )
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
    log_event(
        _log,
        "delivery_report_received",
        outbox_id=str(report.outbox_id),
        delivered=report.status == "delivered",
    )
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
