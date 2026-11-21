from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema, NonBlankStr, PhoneStr

ConversationState = Literal["active", "needs_human", "human_active"]
MessageDirection = Literal["inbound", "agent", "outbound"]
TranscriptRole = Literal["user", "assistant", "tool"]
MessageReadStatus = Literal["unread", "read"]
OutboxStatus = Literal["pending", "sending", "sent", "failed", "unknown"]
ApprovalStatus = Literal["pending_delivery", "active", "consumed"]

# transitions used by the messaging layer; the database CHECK is the
# authority for the state vocabulary, this is the application policy map
HANDOFF_STATES: tuple[ConversationState, ...] = ("needs_human", "human_active")


class ToolCallPayload(BaseSchema):
    id: NonBlankStr
    name: NonBlankStr
    arguments: str  # raw JSON object string, same as AgentToolCall


class ConversationResponse(BaseSchema):
    id: UUID
    phone: PhoneStr
    customer_id: UUID | None
    state: ConversationState
    handoff_reason: str | None
    created_at: datetime
    updated_at: datetime


class ConversationMessageResponse(BaseSchema):
    id: UUID
    conversation_id: UUID
    seq: int = Field(ge=1)
    direction: MessageDirection
    role: TranscriptRole
    content: str | None
    tool_call_id: str | None
    tool_calls: list[ToolCallPayload] | None
    external_message_id: str | None
    status: MessageReadStatus | None
    created_at: datetime


class MessageInsert(BaseSchema):
    direction: MessageDirection
    role: TranscriptRole
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCallPayload] | None = None
    external_message_id: str | None = None
    status: MessageReadStatus | None = None


class OutboxMessageResponse(BaseSchema):
    id: UUID
    message_id: UUID
    conversation_id: UUID
    body: NonBlankStr
    status: OutboxStatus
    attempts: int = Field(ge=0)
    last_error: str | None
    sent_at: datetime | None
    claimed_at: datetime | None = None  # claim lease: stale-detection for 'sending'
    created_at: datetime
    updated_at: datetime


class OrderApprovalResponse(BaseSchema):
    id: UUID
    conversation_id: UUID
    order_id: UUID
    delivery_message_id: UUID
    status: ApprovalStatus
    activated_at: datetime | None
    consumed_at: datetime | None
    created_at: datetime
    updated_at: datetime
