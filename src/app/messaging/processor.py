import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from app.agents.exceptions import AgentError
from app.agents.orchestrator import AgentOrchestrator
from app.agents.schemas import AgentInput, AgentMessage
from app.intent.gate import IntentGate
from app.messaging.approval import ApprovalManager
from app.messaging.conversation_service import ConversationService
from app.messaging.handoff import HandoffManager
from app.messaging.locking import conversation_lock
from app.messaging.outbox import OutboxProcessor
from app.schemas.conversation import ConversationMessageResponse
from app.tools.context import ToolContext
from app.tools.definitions import (
    CONFIRM_ORDER_TOOL_NAME,
    CREATE_DRAFT_ORDER_TOOL_NAME,
)
from app.whatsapp.schemas import WhatsAppInboundMessage

ProcessingStatus = Literal[
    "duplicate",
    "unknown_sender",
    "identity_mismatch",
    "human_handling",
    "handoff",
    "deferred",
    "agent_error",
    "processed",
]

IDENTITY_MISMATCH_REASON = "identity_mismatch"


@dataclass(frozen=True)
class ProcessingOutcome:

    status: ProcessingStatus
    mark_read: bool
    conversation_id: UUID | None = None
    replied: bool = False
    detail: str | None = None


class MessageProcessor:
    def __init__(
        self,
        conversations: ConversationService,
        gate: IntentGate,
        orchestrator: AgentOrchestrator,
        handoff: HandoffManager,
        approvals: ApprovalManager,
        outbox: OutboxProcessor,
        lock_factory: Callable[[str], object] = conversation_lock,
        max_intent_context: int = 10,
        max_transcript_messages: int = 200,
    ) -> None:
        self._conversations = conversations
        self._gate = gate
        self._orchestrator = orchestrator
        self._handoff = handoff
        self._approvals = approvals
        self._outbox = outbox
        self._lock_factory = lock_factory
        self._max_intent_context = max_intent_context
        self._max_transcript_messages = max_transcript_messages

    def process(self, message: WhatsAppInboundMessage) -> ProcessingOutcome:
        with self._lock_factory(message.phone):
            return self._process_locked(message)

    def reprocess_deferred(self, conversation_id: UUID) -> list[ProcessingOutcome]:
        conversation = self._conversations.get_conversation(conversation_id)
        with self._lock_factory(conversation.phone):
            return self._reprocess_locked(conversation)

    # -- internals --------------------------------------------------------

    def _process_locked(self, message: WhatsAppInboundMessage) -> ProcessingOutcome:
        inbound = self._conversations.ingest_inbound(
            message.phone, message.external_message_id, message.text
        )
        conversation = inbound.conversation
        if inbound.message is None:
            # duplicate claim: the stored record authorizes a repeat read
            # receipt only when it was actually read (Phase 4 §5); an
            # unread or cross-conversation record stays unauthorized
            stored = inbound.duplicate
            return ProcessingOutcome(
                status="duplicate",
                mark_read=stored is not None and stored.status == "read",
                conversation_id=conversation.id,
            )
        if inbound.customer is None:
            # unknown sender: stored unread, no reply, no read receipt,
            # no fake customer, no agent
            return ProcessingOutcome(
                status="unknown_sender",
                mark_read=False,
                conversation_id=conversation.id,
            )
        if inbound.identity_mismatch:
            # H4: the transcript belongs to the LINKED customer; the
            # current sender is a different registered customer. Stop,
            # flag for a human, never overwrite the link, never run the
            # agent with the mixed identity.
            self._handoff.request_handoff(
                conversation.id,
                reason=IDENTITY_MISMATCH_REASON,
                acquire_lock=False,  # the phone lock is already held
            )
            return ProcessingOutcome(
                status="identity_mismatch",
                mark_read=False,
                conversation_id=conversation.id,
                detail=IDENTITY_MISMATCH_REASON,
            )
        return self._execute(conversation, inbound.customer, inbound.message)

    def _reprocess_locked(self, conversation) -> list[ProcessingOutcome]:
        outcomes: list[ProcessingOutcome] = []
        for row in self._conversations.unread_inbound(conversation.id):
            customer = self._conversations.customer_by_phone(conversation.phone)
            if customer is None:
                continue  # unknown-sender retention: never replayed
            # H4 holds on reprocessing too: the transcript belongs to the
            # LINKED customer; a different resolved customer stops everything
            linked = self._conversations.get_conversation(conversation.id)
            if linked.customer_id is not None and linked.customer_id != customer.id:
                self._handoff.request_handoff(
                    conversation.id,
                    reason=IDENTITY_MISMATCH_REASON,
                    acquire_lock=False,
                )
                outcomes.append(
                    ProcessingOutcome(
                        status="identity_mismatch",
                        mark_read=False,
                        conversation_id=conversation.id,
                        detail=IDENTITY_MISMATCH_REASON,
                    )
                )
                break
            outcome = self._execute(conversation, customer, row)
            outcomes.append(outcome)
            if outcome.status == "deferred":
                break  # another approval is still pending: stop here
        return outcomes

    def _execute(
        self, conversation, customer, message: ConversationMessageResponse
    ) -> ProcessingOutcome:
        # fresh state at the boundary (H2): the conversation object may
        # be a snapshot from before an operator transition
        conversation = self._conversations.get_conversation(conversation.id)
        if conversation.state != "active":
            # needs_human / human_active: hard stop before the gate
            return ProcessingOutcome(
                status="human_handling",
                mark_read=False,
                conversation_id=conversation.id,
                detail=f"state:{conversation.state}",
            )
        if self._approvals.pending_exists(conversation.id):
            # M1: an order summary is awaiting its delivery confirmation.
            # A reply here is very plausibly a confirmation, but approval
            # is NOT inferred from message arrival: defer (stored unread,
            # no agent run) until the approval activates or a human acts.
            return ProcessingOutcome(
                status="deferred",
                mark_read=False,
                conversation_id=conversation.id,
                detail="approval_pending_delivery",
            )

        context = self._conversations.intent_context(
            conversation.id,
            before_seq=message.seq,
            limit=self._max_intent_context,
        )
        decision = self._gate.evaluate(message.content or "", context)
        if not decision.allow_agent:
            self._handoff.request_handoff(
                conversation.id,
                reason=decision.reason,
                acquire_lock=False,  # the phone lock is already held
            )
            return ProcessingOutcome(
                status="handoff",
                mark_read=False,
                conversation_id=conversation.id,
                detail=decision.reason,
            )

        try:
            agent_input = AgentInput(
                messages=self._conversations.load_agent_messages(
                    conversation.id, self._max_transcript_messages
                )
            )
        except ValidationError as exc:
            # corrupt transcript store: fail loud, fail closed (no agent
            # run, no reply, message stays unread); the message itself is
            # already persisted
            return ProcessingOutcome(
                status="agent_error",
                mark_read=False,
                conversation_id=conversation.id,
                detail=f"transcript:{exc.errors()[0]['type']}",
            )

        approval = self._approvals.latest_active(conversation.id)
        tool_context = ToolContext(
            customer_id=customer.id,
            conversation_id=str(conversation.id),
            approved_order_id=approval.order_id if approval is not None else None,
        )
        try:
            run = self._orchestrator.run(agent_input, tool_context)
        except AgentError as exc:
            new_messages = _slice_new(exc.partial_transcript, agent_input.messages)
            if new_messages:
                # preserve partial tool exchanges; never retry them
                self._conversations.persist_agent_run(conversation.id, new_messages)
            return ProcessingOutcome(
                status="agent_error",
                mark_read=False,  # M2: unsuccessful processing stays unread
                conversation_id=conversation.id,
                detail=type(exc).__name__,
            )

        new_messages = _slice_new(run.transcript, agent_input.messages)
        confirmed = _tool_order_ids(new_messages, CONFIRM_ORDER_TOOL_NAME)
        drafts = _tool_order_ids(new_messages, CREATE_DRAFT_ORDER_TOOL_NAME)
        # H3: transcript rows, the outbox intent, AND the pending_delivery
        # approvals for these drafts are written in ONE transaction
        persisted = (
            self._conversations.persist_agent_run(
                conversation.id, new_messages, draft_order_ids=drafts
            )
            if new_messages
            else None
        )
        if confirmed:
            self._approvals.consume_confirmed(conversation.id, confirmed)

        # M2: read receipt only after successful processing
        self._conversations.mark_message_read(message.id)

        replied = False
        if persisted is not None and persisted.outbound is not None:
            # H2 defense in depth: recheck the state before the reply
            # leaves. Under the phone lock this cannot flip mid-run, but
            # the check keeps the invariant even for lock-less callers.
            fresh = self._conversations.get_conversation(conversation.id)
            if fresh.state != "active":
                # reply stays pending in the outbox for the operator
                return ProcessingOutcome(
                    status="processed",
                    mark_read=True,
                    conversation_id=conversation.id,
                    replied=False,
                    detail="handoff_before_dispatch",
                )
            reports = self._outbox.dispatch_conversation(conversation.id)
            replied = any(r.status in ("sent", "unknown") for r in reports)
            if any(r.activated for r in reports):
                # M1: an approval just activated; messages deferred behind
                # it become processable now (we already hold the lock)
                self._reprocess_locked(conversation)
        return ProcessingOutcome(
            status="processed",
            mark_read=True,
            conversation_id=conversation.id,
            replied=replied,
            detail=decision.reason,
        )


def _slice_new(
    transcript: Sequence[AgentMessage] | None, input_messages: Sequence[AgentMessage]
) -> list[AgentMessage]:
    if transcript is None:
        return []
    return list(transcript[len(input_messages) :])


def _tool_order_ids(new_messages: Sequence[AgentMessage], tool_name: str) -> list[UUID]:
    order_ids: list[UUID] = []
    for message in new_messages:
        if message.role != "tool":
            continue
        try:
            payload = json.loads(message.content or "")
            order_id = payload["data"]["order_id"]
            if payload.get("tool_name") == tool_name and payload.get("ok") is True:
                order_ids.append(UUID(str(order_id)))
        except (ValueError, KeyError, TypeError):
            continue
    return order_ids
