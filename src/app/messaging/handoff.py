import logging
from collections.abc import Callable
from uuid import UUID

from app.messaging.conversation_service import ConversationService
from app.messaging.locking import conversation_lock
from app.observability import log_event
from app.schemas.conversation import ConversationResponse

_log = logging.getLogger(__name__)


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
            log_event(
                _log,
                "handoff_requested",
                level=logging.WARNING,
                conversation_id=str(conversation_id),
                reason=reason,
                already_flagged=True,
            )
            return conversation  # idempotent: already flagged
        log_event(
            _log,
            "handoff_requested",
            level=logging.WARNING,
            conversation_id=str(conversation_id),
            reason=reason,
        )
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
