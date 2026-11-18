"""HandoffManager: the human-handoff state policy.

States and their purpose (no others exist):
- active: the agent may process messages automatically.
- needs_human: a handoff was requested (intent gate or operator); NO
  automatic processing, replies, or read receipts; inbound messages are
  persisted unread for the operator.
- human_active: an operator has taken the conversation over (same hard
  stop as needs_human; distinguishes "flagged" from "being handled").

Transitions:
- active|human_active -> needs_human: request_handoff (idempotent when
  already needs_human).
- needs_human -> human_active: takeover (operator acknowledged).
- needs_human|human_active -> active: resume (EXPLICIT human action only;
  nothing in the inbound path ever resumes automatically).

Serialization (H2): operator transitions take the SAME per-phone advisory
lock as inbound processing (conversation_lock). A handoff requested while
an agent run is in flight waits for the run to finish, then applies: the
run's reply goes out first, and every message AFTER the transition hits
the hard stop. This gives state changes and inbound execution one
well-defined order without holding a database transaction (let alone a
row lock) across the agent's network calls.

The processor calls these methods with acquire_lock=False when it
already holds the phone lock: pg_advisory_lock is session-scoped, and a
second lock attempt on a NEW connection for the same key would
self-deadlock.

The hard stop itself is enforced by MessageProcessor (it checks the
state before the gate and rechecks before dispatching a reply); this
class only owns the transition rules. Authorization for these
operations is an API-layer boundary (app.api.security).
"""

from collections.abc import Callable
from uuid import UUID

from app.messaging.conversation_service import ConversationService
from app.messaging.locking import conversation_lock
from app.schemas.conversation import ConversationResponse


class HandoffManager:
    def __init__(
        self,
        conversations: ConversationService,
        lock_factory: Callable[[str], object] = conversation_lock,
    ) -> None:
        self._conversations = conversations
        self._lock_factory = lock_factory

    def request_handoff(
        self,
        conversation_id: UUID,
        reason: str | None = None,
        acquire_lock: bool = True,
    ) -> ConversationResponse:
        conversation = self._conversations.get_conversation(conversation_id)
        if conversation.state == "needs_human":
            return conversation  # idempotent: already flagged
        if not acquire_lock:
            return self._conversations.transition_state(
                conversation_id, ("active", "human_active"), "needs_human", reason
            )
        with self._lock_factory(conversation.phone):
            return self._conversations.transition_state(
                conversation_id, ("active", "human_active"), "needs_human", reason
            )

    def takeover(
        self, conversation_id: UUID, acquire_lock: bool = True
    ) -> ConversationResponse:
        conversation = self._conversations.get_conversation(conversation_id)
        if not acquire_lock:
            return self._conversations.transition_state(
                conversation_id, ("needs_human",), "human_active"
            )
        with self._lock_factory(conversation.phone):
            return self._conversations.transition_state(
                conversation_id, ("needs_human",), "human_active"
            )

    def resume(
        self, conversation_id: UUID, acquire_lock: bool = True
    ) -> ConversationResponse:
        conversation = self._conversations.get_conversation(conversation_id)
        if not acquire_lock:
            return self._conversations.transition_state(
                conversation_id, ("needs_human", "human_active"), "active"
            )
        with self._lock_factory(conversation.phone):
            return self._conversations.transition_state(
                conversation_id, ("needs_human", "human_active"), "active"
            )
