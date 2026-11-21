import logging
from dataclasses import dataclass
from uuid import UUID

from app.agents.schemas import AgentMessage, AgentToolCall
from app.database.connection import transaction
from app.messaging.exceptions import (
    ConversationNotFoundError,
    InvalidConversationStateError,
)
from app.messaging.identity import IdentityResolver
from app.observability import log_event
from app.repositories.approval import ApprovalRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.conversation import (
    ConversationMessageResponse,
    ConversationResponse,
    ConversationState,
    MessageInsert,
    OutboxMessageResponse,
)
from app.schemas.customer import CustomerResponse

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundResult:
    conversation: ConversationResponse
    message: ConversationMessageResponse | None
    customer: CustomerResponse | None
    identity_mismatch: bool = False
    # Phase 4: the stored record behind a lost duplicate claim. Only set
    # when it belongs to THIS conversation; never set for a new message.
    duplicate: ConversationMessageResponse | None = None


@dataclass(frozen=True)
class PersistedRun:
    messages: list[ConversationMessageResponse]
    outbound: OutboxMessageResponse | None


class ConversationService:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        outbox: OutboxRepository,
        identity: IdentityResolver,
        approvals: ApprovalRepository | None = None,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._outbox = outbox
        self._identity = identity
        self._approvals = approvals

    # -- inbound ---------------------------------------------------------

    def ingest_inbound(
        self, phone: str, external_message_id: str, text: str
    ) -> InboundResult:
        with transaction() as conn:
            customer = self._identity.resolve(conn, phone)
            conversation = self._conversations.get_or_create_by_phone(
                conn, phone, customer.id if customer else None
            )
            if customer is not None and conversation.customer_id is None:
                # conversation predates registration: link now; old
                # messages stay stored but are NOT replayed to the agent
                conversation = (
                    self._conversations.link_customer(
                        conn, conversation.id, customer.id
                    )
                    or conversation
                )
            mismatch = (
                customer is not None
                and conversation.customer_id is not None
                and conversation.customer_id != customer.id
            )
            message = self._messages.claim_inbound(
                conn, conversation.id, external_message_id, text
            )
            duplicate = None
            if message is None:
                stored = self._messages.get_by_external_id(conn, external_message_id)
                if stored is not None and stored.conversation_id == conversation.id:
                    duplicate = stored
        log_event(
            _log,
            "conversation_resolved",
            conversation_id=str(conversation.id),
            state=conversation.state,
            customer_id=(
                str(conversation.customer_id)
                if conversation.customer_id is not None
                else None
            ),
        )
        return InboundResult(conversation, message, customer, mismatch, duplicate)

    def customer_by_phone(self, phone: str) -> CustomerResponse | None:
        with transaction() as conn:
            return self._identity.resolve(conn, phone)

    def mark_message_read(self, message_id: UUID) -> bool:
        with transaction() as conn:
            row = self._messages.mark_read(conn, message_id)
        return row is not None

    # -- transcript ------------------------------------------------------

    def intent_context(
        self, conversation_id: UUID, before_seq: int, limit: int
    ) -> list[tuple[str, str]]:
        if limit <= 0:
            return []
        with transaction() as conn:
            rows = self._messages.list_by_conversation(
                conn,
                conversation_id,
                before_seq=before_seq,
                limit=limit,
                roles=("user", "assistant"),
            )
        return [
            (row.role, row.content)
            for row in rows
            if row.content is not None and row.role in ("user", "assistant")
        ]

    def load_transcript(self, conversation_id: UUID) -> list[AgentMessage]:
        with transaction() as conn:
            rows = self._messages.list_by_conversation(conn, conversation_id)
        return [_to_agent_message(row) for row in rows]

    def load_agent_messages(
        self, conversation_id: UUID, limit: int
    ) -> list[AgentMessage]:
        with transaction() as conn:
            rows = self._messages.list_by_conversation(conn, conversation_id)
        rows = rows[-limit:] if limit < len(rows) else rows
        while rows and rows[0].role == "tool":
            rows = rows[1:]  # orphaned tool result: no paired tool call
        return [_to_agent_message(row) for row in rows]

    def unread_inbound(
        self, conversation_id: UUID
    ) -> list[ConversationMessageResponse]:
        with transaction() as conn:
            return self._messages.list_unread_inbound(conn, conversation_id)

    def persist_agent_run(
        self,
        conversation_id: UUID,
        new_messages: list[AgentMessage],
        draft_order_ids: list[UUID] | None = None,
    ) -> PersistedRun:
        drafts = draft_order_ids or []
        inserts = [_to_insert(m) for m in new_messages]
        with transaction() as conn:
            rows = [
                self._messages.append_message(conn, conversation_id, insert)
                for insert in inserts
            ]
            outbound: OutboxMessageResponse | None = None
            for row in rows:
                if row.direction == "outbound" and row.content:
                    outbound = self._outbox.create(
                        conn, row.id, conversation_id, row.content
                    )
            if outbound is not None:
                log_event(
                    _log,
                    "outbox_message_created",
                    outbox_id=str(outbound.id),
                    conversation_id=str(conversation_id),
                )
            if outbound is not None and drafts and self._approvals is not None:
                for order_id in drafts:
                    pending = self._approvals.create_pending(
                        conn, conversation_id, order_id, outbound.id
                    )
                    if pending is not None:
                        log_event(
                            _log,
                            "approval_registered",
                            order_id=str(order_id),
                            outbox_id=str(outbound.id),
                            conversation_id=str(conversation_id),
                        )
        return PersistedRun(rows, outbound)

    # -- conversation state / listing ------------------------------------

    def get_conversation(self, conversation_id: UUID) -> ConversationResponse:
        with transaction() as conn:
            row = self._conversations.get_by_id(conn, conversation_id)
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        return row

    def list_conversations(
        self,
        state: ConversationState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationResponse]:
        with transaction() as conn:
            return self._conversations.list_conversations(conn, state, limit, offset)

    def list_messages(self, conversation_id: UUID) -> list[ConversationMessageResponse]:
        self.get_conversation(conversation_id)  # 404 for unknown ids
        with transaction() as conn:
            return self._messages.list_by_conversation(conn, conversation_id)

    def transition_state(
        self,
        conversation_id: UUID,
        from_states: tuple[ConversationState, ...],
        to_state: ConversationState,
        reason: str | None = None,
    ) -> ConversationResponse:
        with transaction() as conn:
            current = self._conversations.get_by_id(conn, conversation_id)
            if current is None:
                raise ConversationNotFoundError(conversation_id)
            if current.state not in from_states:
                raise InvalidConversationStateError(
                    conversation_id, current.state, to_state
                )
            updated = self._conversations.set_state(
                conn, conversation_id, from_states, to_state, reason
            )
            if updated is None:  # raced between check and guarded update
                fresh = self._conversations.get_by_id(conn, conversation_id)
                raise InvalidConversationStateError(
                    conversation_id,
                    fresh.state if fresh else to_state,
                    to_state,
                )
        return updated


def _to_agent_message(row: ConversationMessageResponse) -> AgentMessage:
    return AgentMessage(
        role=row.role,
        content=row.content,
        tool_call_id=row.tool_call_id,
        tool_calls=(
            [AgentToolCall.model_validate(call) for call in row.tool_calls]
            if row.tool_calls is not None
            else None
        ),
    )


def _to_insert(message: AgentMessage) -> MessageInsert:
    if message.role == "user":
        raise ValueError(
            "persist_agent_run stores agent output only; "
            "inbound messages are claimed via ingest_inbound"
        )
    if message.role == "tool":
        return MessageInsert(
            direction="agent",
            role="tool",
            content=message.content,
            tool_call_id=message.tool_call_id,
        )
    if message.tool_calls is not None:
        return MessageInsert(
            direction="agent",
            role="assistant",
            content=None,
            tool_calls=[
                AgentToolCall.model_validate(
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
                for call in message.tool_calls
            ],
        )
    return MessageInsert(
        direction="outbound", role="assistant", content=message.content
    )
