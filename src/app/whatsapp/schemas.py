"""Strict schemas for the WhatsApp bridge contract.

The RAW Baileys event is parsed leniently (dict traversal in
normalization.py: real events carry many extra protocol fields); these
models are the STRICT outputs of that normalization and the strict
inputs the API layer accepts from the bridge.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema, NonBlankStr, PhoneStr


class WhatsAppInboundMessage(BaseSchema):
    """One normalized inbound customer message."""

    external_message_id: NonBlankStr
    phone: PhoneStr
    text: NonBlankStr
    received_at: datetime | None = None


class WhatsAppDeliveryReport(BaseSchema):
    """Final delivery outcome for one outbound message, as reported by
    the bridge. status 'delivered' means the message reached the
    recipient device (WhatsApp DELIVERY_ACK/READ); 'failed' means the
    bridge knows it did not go out or failed permanently."""

    outbox_id: UUID
    status: Literal["delivered", "failed"]
    reported_at: datetime | None = None
    error: str | None = None


class WhatsAppEventAck(BaseSchema):
    """Webhook response: what the application did with the event, and
    whether the bridge may send a read receipt (mark_read False = the
    application instructs the bridge to suppress the receipt)."""

    external_message_id: str | None = None
    status: str
    mark_read: bool = False
    replied: bool = False


class OutboxDispatchSummary(BaseSchema):
    """Result of a dispatch sweep (manual or scheduled)."""

    dispatched: int = Field(ge=0)
    results: list[dict] = Field(default_factory=list)


class DeliveryReportAck(BaseSchema):
    outbox_id: UUID
    status: str
