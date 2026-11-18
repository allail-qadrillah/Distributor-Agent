from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema, NonBlankStr, PhoneStr


class WhatsAppInboundMessage(BaseSchema):

    external_message_id: NonBlankStr
    phone: PhoneStr
    text: NonBlankStr
    received_at: datetime | None = None


class WhatsAppDeliveryReport(BaseSchema):

    outbox_id: UUID
    status: Literal["delivered", "failed"]
    reported_at: datetime | None = None
    error: str | None = None


class WhatsAppEventAck(BaseSchema):

    external_message_id: str | None = None
    status: str
    mark_read: bool = False
    replied: bool = False


class OutboxDispatchSummary(BaseSchema):

    dispatched: int = Field(ge=0)
    results: list[dict] = Field(default_factory=list)


class DeliveryReportAck(BaseSchema):
    outbox_id: UUID
    status: str
