"""ConversationService: persistence and reconstruction of one permanent
conversation per WhatsApp phone.

Workflows (one transaction each, services own the boundary):
- ingest_inbound: resolve identity, get-or-create the conversation,
  atomically claim the external message id. The claim INSERT is the
  duplicate protection (ON CONFLICT DO NOTHING), not a SELECT-then-INSERT.
- intent_context: the tail of the conversation (user and assistant text
  turns only) for the intent classifier, newest last.
- load_transcript: the full stored transcript as AgentMessage list, the
  exact replay input for the next agent run (tool calls and tool results
  included; pairing is revalidated by AgentInput).
- persist_agent_run: append one run's new transcript messages and create
  the outbox row for the outbound reply in the SAME transaction, so the
  reply intent exists before anything is sent.
- mark_message_read / transition_state / listing: read and guarded state
  workflows for the processor and the API layer.

Unknown sender retention: a conversation row exists per phone even with
customer_id NULL; messages stay unread and are never replayed into an
agent run (the processor refuses to run without a resolved customer).
"""

from dataclasses import dataclass
from uuid import UUID

from app.agents.schemas import AgentMessage, AgentToolCall
from app.database.connection import transaction
from app.messaging.exceptions import (
    ConversationNotFoundError,
    InvalidConversationStateError,
)
from app.messaging.identity import IdentityResolver
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


@dataclass(frozen=True)
class InboundResult:
    """Outcome of ingest_inbound. message is None for a duplicate external
    message id (already stored; the caller must not process again).
    identity_mismatch is True when the phone now resolves to a DIFFERENT
    customer than the one the conversation is permanently linked to: the
    caller must stop automatic processing and hand off (the persisted
    transcript belongs to the linked customer, not the current sender)."""

    conversation: ConversationResponse
    message: ConversationMessageResponse | None
    customer: CustomerResponse | None
    identity_mismatch: bool = False


@dataclass(frozen=True)
class PersistedRun:
    """One persisted agent run. outbound is the outbox row created for the
    final assistant text (None when the run produced no reply text)."""

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
        """Resolve identity, ensure the conversation, claim the message.
        The inbound row is persisted BEFORE any processing: an error later
        never loses the message.

        Identity invariant: once a conversation is linked to a customer,
        that link is the identity of the transcript. If the phone now
        resolves to a different registered customer, identity_mismatch is
        True — the link is NEVER overwritten and the caller must stop
        automatic processing (H4)."""
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
        return InboundResult(conversation, message, customer, mismatch)

    def customer_by_phone(self, phone: str) -> CustomerResponse | None:
        """The registered customer for a phone (None = unknown sender).
        Used when reprocessing stored messages, where ingest already ran."""
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
        """Conversation context for the classifier: the last `limit` user
        and assistant TEXT turns before seq, oldest first. Tool protocol
        turns are deliberately excluded (intent needs meaning, not
        mechanics)."""
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
        """Full transcript, oldest first, as AgentMessage: the replayable
        protocol including tool calls and tool results."""
        with transaction() as conn:
            rows = self._messages.list_by_conversation(conn, conversation_id)
        return [_to_agent_message(row) for row in rows]

    def load_agent_messages(
        self, conversation_id: UUID, limit: int
    ) -> list[AgentMessage]:
        """Bounded replay input (M5): the NEWEST `limit` transcript
        messages as AgentMessage, valid for AgentInput. The full
        transcript stays persisted untouched (audit); only the agent's
        view is bounded. Leading orphaned tool results (whose assistant
        tool-call turn fell outside the window) are dropped so pairing
        stays valid — AgentInput revalidates the rest.

        ponytail: windowing in Python over one query; push the window
        into SQL only if conversation length makes the full read hurt."""
        with transaction() as conn:
            rows = self._messages.list_by_conversation(conn, conversation_id)
        rows = rows[-limit:] if limit < len(rows) else rows
        while rows and rows[0].role == "tool":
            rows = rows[1:]  # orphaned tool result: no paired tool call
        return [_to_agent_message(row) for row in rows]

    def unread_inbound(
        self, conversation_id: UUID
    ) -> list[ConversationMessageResponse]:
        """Stored unread INBOUND messages, oldest first: the deferred
        messages waiting for approval activation / human action."""
        with transaction() as conn:
            return self._messages.list_unread_inbound(conn, conversation_id)

    def persist_agent_run(
        self,
        conversation_id: UUID,
        new_messages: list[AgentMessage],
        draft_order_ids: list[UUID] | None = None,
    ) -> PersistedRun:
        """Append one run's new transcript messages, create the outbound
        intent, AND register the pending_delivery approvals for the run's
        draft orders — all in ONE transaction (H3): a crash can never
        leave a sent order summary without its approval row. User-role
        messages are rejected: the inbound message was already claimed in
        ingest_inbound. Drafts are registered only when a summary reply
        exists (approvals are bound to the delivery of that exact
        message; no reply text means nothing to deliver)."""
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
            if outbound is not None and drafts and self._approvals is not None:
                for order_id in drafts:
                    self._approvals.create_pending(
                        conn, conversation_id, order_id, outbound.id
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
        """Checked + guarded transition: not-found raises, a wrong current
        state raises, the guarded UPDATE decides under concurrency."""
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
    """Stored row -> protocol message. Tool calls ride as plain dicts from
    jsonb; AgentMessage revalidates the full per-role shape, so a corrupt
    store fails here, loudly, instead of at the provider."""
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
